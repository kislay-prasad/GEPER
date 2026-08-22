"""
pipeline/orchestration/shared.py
─────────────────────────────────
Shared orchestration helpers used by BOTH entry points:

  * PipelineRunner.run() (the `analyze` FASTQ→Report workflow, in
    pipeline/orchestration/runner.py)
  * cmd_vcf() (the `vcf` annotate-and-classify workflow, in main.py)

This module exists so the ACMG + ClinVar + gnomAD + AI evidence-scoring
logic is implemented exactly once. It was previously inlined only inside
PipelineRunner.run(), which meant `python main.py vcf` silently skipped
ClinVar, gnomAD, PGx-relevant constraint/hotspot lookups, and the AI engine
entirely. Extracting it here lets both code paths share the identical,
already-tested logic — no behavior change for `analyze`, full wiring for
`vcf`.

NOTE: this is a refactor (move), not a rewrite. The per-variant evidence
construction and ACMG classification logic below is unchanged from the
original inline implementation in runner.py.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("geper.pipeline.orchestration.shared")


def _availability_fields(
    gene: str,
    gene_unavailable_reason: str,
    clinvar_enabled: bool,
    clinvar_significance: str | None,
    gnomad_enabled: bool,
    gnomad_af: float | None,
    gnomad_af_popmax: float | None,
    gnomad_af_absent: bool | None,
) -> dict:
    """FIX (Issue 7): build the reporting-facing availability/reason fields
    for one variant's gene / ClinVar / gnomAD annotation, so downstream
    reports never have to silently render a null as a blank cell — every
    unavailable field carries a clear, specific reason.
    """
    if gene:
        gene_status, gene_reason = "resolved", None
    else:
        gene_status, gene_reason = "unavailable", gene_unavailable_reason

    if not clinvar_enabled:
        clinvar_status, clinvar_reason = "disabled", "ClinVar lookup skipped (disabled)"
    elif clinvar_significance is None:
        clinvar_status, clinvar_reason = "not_found", "Variant not found in ClinVar"
    else:
        clinvar_status, clinvar_reason = "found", None

    if not gnomad_enabled:
        gnomad_status, gnomad_reason = "disabled", "gnomAD lookup skipped (disabled)"
    elif gnomad_af is None and gnomad_af_popmax is None:
        if gnomad_af_absent:
            gnomad_status, gnomad_reason = "absent", "Confirmed absent from gnomAD"
        else:
            gnomad_status, gnomad_reason = (
                "unavailable",
                "gnomAD lookup unavailable (network/tabix error)",
            )
    else:
        gnomad_status, gnomad_reason = "found", None

    return {
        "gene_status": gene_status,
        "gene_unavailable_reason": gene_reason,
        "clinvar_status": clinvar_status,
        "clinvar_unavailable_reason": clinvar_reason,
        "gnomad_status": gnomad_status,
        "gnomad_unavailable_reason": gnomad_reason,
        # FIX (Issue 2 / gnomAD reliability): the dict returned here is
        # what ultimately reaches the HTML/JSON report (see
        # pipeline/reporting/stage.py). Previously only a found/not-found
        # *status* was threaded through — the actual frequency value was
        # computed upstream but then silently dropped before it ever
        # reached the report, so gnomAD AFs were never actually visible
        # to a clinician even when the lookup succeeded.
        "gnomad_af": gnomad_af,
        "gnomad_af_popmax": gnomad_af_popmax,
    }


def run_acmg_evidence_batch(
    ann_variants: list[dict],
    cfg: dict,
    sample_id: str = "SAMPLE",
    pedigree_data: dict | None = None,
    global_inheritance: str | None = None,
) -> list[dict]:
    """Run ClinVar + gnomAD + constraint/hotspot + AI lookups, build
    VariantEvidence for every annotated variant, classify with ACMG, and
    aggregate the final evidence score.

    Args:
        ann_variants: list of annotated-variant dicts (as produced by
            AnnotationStage / VEPAnnotationStage), each containing at
            least chrom/pos/ref/alt and optionally gene_name, consequence,
            cadd_phred, revel_score, spliceai_score, alphamissense_score,
            wildtype_aa, mutant_aa, ref_sequence, alt_sequence, etc.
        cfg: full pipeline config dict.
        sample_id: sample identifier, used only for logging.
        pedigree_data: optional dict keyed by "chrom:pos:ref:alt" with
            pedigree/phenotype evidence fields (PS2/PM6/PP1/BS4/PP4/PM3/
            BP5/PS2-like fields). Pass None if no pedigree sidecar exists
            (this is the normal case for standalone VCF annotation).
        global_inheritance: fallback inheritance pattern when a per-variant
            pedigree entry doesn't specify one.

    Returns:
        List of dicts, one per variant, each the merge of the ACMG
        classification result and the aggregated evidence result
        (or an {"error": ...} record if classification failed for that
        variant — a single bad variant never aborts the whole batch).
    """
    from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence
    from pipeline.clingen.lookup import ClinGenDosageLookup
    from pipeline.clinvar.lookup import ClinVarLookup
    from pipeline.constraint.lookup import GnomadConstraintLookup
    from pipeline.evidence.aggregator import EvidenceAggregator
    from pipeline.gnomad.lookup import GnomadHit, GnomadLookup, GnomadLookupOutcome
    from pipeline.hotspot.lookup import HotspotLookup

    pedigree_data = pedigree_data or {}

    acmg_clf = AcmgClassifier(cfg=cfg)
    evidence_agg = EvidenceAggregator(cfg=cfg)
    clinvar_lkp = ClinVarLookup(cfg=cfg)
    gnomad_lkp = GnomadLookup(cfg=cfg)
    # Batch-prefetch all gnomAD lookups up front (bounded concurrency,
    # dedup'd, disk-cached — see pipeline/gnomad/lookup.py::lookup_batch)
    # instead of the ACMG loop below hitting the network one variant at a
    # time. The loop's own `gnomad_lkp.lookup(...)` calls are otherwise
    # untouched — they simply become cache hits against what was just
    # prefetched here, so this is a purely additive change to orchestration.
    try:
        gnomad_lkp.lookup_batch(
            [
                (v.get("chrom", ""), int(v.get("pos", 0)), v.get("ref", ""), v.get("alt", ""))
                for v in ann_variants
            ]
        )
    except Exception as batch_exc:
        # A prefetch failure must never abort the run — the per-variant
        # loop below still works correctly (just without the network
        # speedup), each falling back to its own individual lookup().
        logger.warning(
            "[%s] gnomAD batch prefetch failed (continuing per-variant): %s", sample_id, batch_exc
        )
    # ClinGen Dosage Sensitivity: LoF-intolerance fallback for genes with
    # no gnomAD pLI/LOEUF coverage (see pipeline/clingen/lookup.py).
    clingen_lkp = ClinGenDosageLookup(cfg=cfg)
    constraint_lkp = GnomadConstraintLookup(cfg=cfg, clingen_fallback=clingen_lkp)
    hotspot_lkp = HotspotLookup(cfg=cfg)

    # FIX (Issue 7): reports must never silently show "gene = null" or a
    # bare unexplained blank for ClinVar/gnomAD — every unavailable field
    # needs a stated reason. Determine *why* gene annotation may be
    # unavailable up front, from config, so it can be attached to every
    # variant that has no resolved gene.
    vep_cfg = (cfg or {}).get("vep", {}) or {}
    vep_enabled = bool(vep_cfg.get("enabled", True))
    # Single source of truth shared with AnnotationStage (see
    # mt_gff3_bootstrap.py's docstring) — this must never independently
    # decide "is a GFF3 available" differently than the stage that
    # actually resolves/loads one, or the reason shown here could
    # contradict what annotation actually did.
    from pipeline.annotation.mt_gff3_bootstrap import resolve_gff3_source

    _gff_path, gff_provenance = resolve_gff3_source(cfg)
    if not vep_enabled:
        gene_unavailable_reason = "VEP disabled"
    elif gff_provenance == "unavailable":
        gene_unavailable_reason = "No GFF3/VEP annotation source configured"
    elif gff_provenance == "mt_bootstrap":
        gene_unavailable_reason = (
            "Variant not resolved to a known gene — only mitochondrial (MT/chrM) genes "
            "are covered by the auto-fetched bootstrap annotation currently active; set "
            "'rna_analysis.refseq_gff' to a full-genome GFF3 for nuclear-genome variants"
        )
    else:
        gene_unavailable_reason = (
            "Variant not resolved to a known gene (intergenic or annotation gap)"
        )

    # ── AI engine (optional) ──────────────────────────────────────────
    _ai_engine = None
    try:
        from pipeline.ai.engine import AiEngine

        _ai_engine = AiEngine(cfg=cfg)
    except Exception:
        pass  # torch/transformers not installed — AI stream disabled

    acmg_batch: list[dict] = []
    _total_variants = len(ann_variants)

    for _variant_idx, variant in enumerate(ann_variants, start=1):
        _variant_t0 = time.monotonic()
        chrom = variant.get("chrom", "")
        pos_v = int(variant.get("pos", 0))
        ref_v = variant.get("ref", "")
        alt_v = variant.get("alt", "")
        logger.info(
            "[PIPELINE:VARIANT] START idx=%d/%d %s:%s %s>%s",
            _variant_idx,
            _total_variants,
            chrom,
            pos_v,
            ref_v,
            alt_v,
        )
        gene = variant.get("gene_name") or variant.get("gene") or ""
        consequence = variant.get("consequence", "").lower()

        # gnomAD lookup — returns GnomadHit, ABSENT, or UNAVAILABLE.
        #
        # None = absence NOT EVALUATED (the backend was unreachable, or the
        # lookup raised). It must not be read as "checked, and not absent",
        # which is what `False` means here — see
        # pipeline/acmg/classifier.py::_pm2 / STATUS_NOT_EVALUATED. Same
        # sentinel convention as `in_hotspot` below, and the shape d500f32
        # established for PM1 when hotspot data was unavailable.
        #
        # GnomadLookupOutcome's own docstring calls these "the three states
        # that the caller needs to differentiate"; collapsing two of them
        # into `False` is what this distinction exists to prevent.
        gnomad_af = None
        gnomad_af_popmax = None
        gnomad_af_absent: bool | None = None
        try:
            gn_result = gnomad_lkp.lookup(chrom, pos_v, ref_v, alt_v)
            if isinstance(gn_result, GnomadHit):
                gnomad_af = gn_result.af
                gnomad_af_popmax = gn_result.af_popmax
                # A real answer: the variant was found, so it is not absent.
                gnomad_af_absent = False
            elif gn_result == GnomadLookupOutcome.ABSENT:
                gnomad_af_absent = True
            else:  # UNAVAILABLE — nothing was checked, so nothing is known.
                gnomad_af_absent = None
        except Exception as gnomad_exc:
            # Stays None (not evaluated) — the lookup itself failed, so this
            # variant's presence in gnomAD is unknown, not disproved.
            gnomad_af_absent = None
            # WARNING, not debug: this determines whether gnomAD evidence
            # reaches ACMG at all, so an operator must see it without
            # re-running at debug level.
            logger.warning(
                "[%s] gnomAD lookup failed for %s:%s %s>%s — gnomAD evidence "
                "not evaluated for this variant: %s: %s",
                sample_id,
                chrom,
                pos_v,
                ref_v,
                alt_v,
                type(gnomad_exc).__name__,
                gnomad_exc,
            )

        # ClinVar lookup
        cv_hit = None
        clinvar_significance = None
        clinvar_stars = 0
        clinvar_conflicting = False
        try:
            cv_hit = clinvar_lkp.lookup(chrom, pos_v, ref_v, alt_v)
            if cv_hit:
                clinvar_significance = cv_hit.significance
                clinvar_stars = cv_hit.review_stars
                clinvar_conflicting = cv_hit.conflicting
        except Exception as clinvar_exc:
            logger.debug(
                "[%s] ClinVar lookup failed for %s:%s %s>%s: %s",
                sample_id,
                chrom,
                pos_v,
                ref_v,
                alt_v,
                clinvar_exc,
            )

        is_lof = consequence in {
            "stop_gained",
            "stop_lost",
            "start_lost",
            "frameshift_variant",
            "splice_donor_variant",
            "splice_acceptor_variant",
        }
        is_missense = consequence == "missense_variant"
        lof_intolerant = False
        # None = hotspot status not evaluated (lookup raised, or neither
        # ClinVar TSV nor UniProt domains BED was loaded) — must NOT be
        # read as a confirmed "not a hotspot" by PM1 (see
        # pipeline/acmg/classifier.py::_pm1 / STATUS_NOT_EVALUATED).
        in_hotspot = None
        _missense_constrained = False
        try:
            if gene:
                lof_intolerant = constraint_lkp.is_lof_intolerant(gene)
                _missense_constrained = constraint_lkp.is_missense_constrained(gene)
            # hotspot_lkp.is_in_hotspot() returns None when hotspot status
            # is unknown (no local data loaded) — `None and is_missense`
            # short-circuits to None, correctly preserving "not evaluated"
            # rather than collapsing it into a false "not met".
            in_hotspot = hotspot_lkp.is_in_hotspot(chrom, pos_v, gene) and is_missense
        except Exception as constraint_exc:
            logger.debug(
                "[%s] gnomAD constraint/hotspot lookup failed for gene %s: %s",
                sample_id,
                gene,
                constraint_exc,
            )
            # in_hotspot stays None (not evaluated) — the lookup itself failed.

        # ── PS1 / PM5: codon-level ClinVar evidence ────────────────────
        same_aa_pathogenic = variant.get("same_aa_pathogenic")
        novel_aa_at_known = variant.get("novel_aa_at_known_pathogenic_codon")
        if is_missense and (same_aa_pathogenic is None or novel_aa_at_known is None):
            try:
                wt_aa = variant.get("wildtype_aa")
                mt_aa = variant.get("mutant_aa")
                _ps1, _pm5 = clinvar_lkp.check_same_codon_pathogenic(
                    chrom, pos_v, ref_v, alt_v, wt_aa, mt_aa
                )
                if same_aa_pathogenic is None:
                    same_aa_pathogenic = _ps1
                if novel_aa_at_known is None:
                    novel_aa_at_known = _pm5
            except Exception as ps1_pm5_exc:
                logger.debug(
                    "[%s] PS1/PM5 codon lookup failed for %s:%s %s>%s: %s",
                    sample_id,
                    chrom,
                    pos_v,
                    ref_v,
                    alt_v,
                    ps1_pm5_exc,
                )

        in_repeat_region = variant.get("in_repeat_region")

        _ped_variant_key = f"{chrom}:{pos_v}:{ref_v}:{alt_v}"
        _ped_v: dict = pedigree_data.get(_ped_variant_key, {})
        _confirmed_de_novo = _ped_v.get("confirmed_de_novo")
        _assumed_de_novo = _ped_v.get("assumed_de_novo")
        _segregates_with_disease = _ped_v.get("segregates_with_disease")
        _segregates_away = _ped_v.get("segregates_away_from_disease")
        _phenotype_specific = _ped_v.get("phenotype_specific_for_gene")
        _in_trans_with_pathogenic = _ped_v.get("in_trans_with_pathogenic")
        _in_trans_or_cis = _ped_v.get("in_trans_or_cis_with_pathogenic_unexpected")
        _alternate_molecular = _ped_v.get("alternate_molecular_basis_found")
        _inheritance = _ped_v.get("inheritance_pattern") or global_inheritance

        ev = VariantEvidence(
            chrom=chrom,
            pos=pos_v,
            ref=ref_v,
            alt=alt_v,
            gene=gene or None,
            gnomad_af=gnomad_af,
            gnomad_af_popmax=gnomad_af_popmax,
            gnomad_af_absent=gnomad_af_absent,
            cadd_phred=variant.get("cadd_phred"),
            revel_score=variant.get("revel_score"),
            spliceai_score=variant.get("spliceai_score"),
            alphamissense_score=variant.get("alphamissense_score"),
            clinvar_significance=clinvar_significance,
            clinvar_stars=clinvar_stars,
            clinvar_conflicting=clinvar_conflicting,
            lof_gene_intolerant=lof_intolerant,
            is_missense=is_missense,
            is_lof=is_lof,
            is_inframe_indel=variant.get("is_inframe_indel", False),
            in_hotspot=in_hotspot,
            same_aa_pathogenic=same_aa_pathogenic,
            novel_aa_at_known_pathogenic_codon=novel_aa_at_known,
            in_repeat_region=in_repeat_region,
            confirmed_de_novo=_confirmed_de_novo,
            assumed_de_novo=_assumed_de_novo,
            segregates_with_disease=_segregates_with_disease,
            segregates_away_from_disease=_segregates_away,
            phenotype_specific_for_gene=_phenotype_specific,
            in_trans_with_pathogenic=_in_trans_with_pathogenic,
            in_trans_or_cis_with_pathogenic_unexpected=_in_trans_or_cis,
            alternate_molecular_basis_found=_alternate_molecular,
            inheritance_pattern=_inheritance,
            # Fixed 2026-08-22, found while removing SpliceAI (licence):
            # `(x or 0.0)` treated an ABSENT spliceai_score identically to
            # a CONFIRMED "no splice impact" score of 0.0, so BP7
            # (`pipeline/acmg/classifier.py::_bp7`) would fire on this
            # signal alone for every synonymous/intronic variant once
            # SpliceAI's plugin is removed upstream and spliceai_score is
            # always absent. Requiring the score to actually be present
            # matches the fail-safe direction every other "missing
            # evidence" check in this codebase already uses (see PP3/BP4's
            # own STATUS_NOT_EVALUATED-on-no-votes) -- absence must not
            # read as a confirmed favorable result. Net effect once
            # SpliceAI is fully removed: this condition is always False,
            # so synonymous_or_intronic no longer fires on splice-safety
            # grounds at all (a real, disclosed behaviour change, not
            # silently absorbed -- see the SpliceAI-removal report).
            synonymous_or_intronic=(
                consequence in {"synonymous_variant", "intron_variant"}
                and variant.get("spliceai_score") is not None
                and variant.get("spliceai_score") < 0.2
            ),
            bp1_reputable_source_benign=(
                clinvar_significance is not None
                and "benign" in clinvar_significance.lower()
                and clinvar_stars >= 1
                and not clinvar_conflicting
            )
            or None,
            missense_constrained=_missense_constrained,
        )

        try:
            acmg_res = acmg_clf.classify(ev)

            # ── AI scoring (per-variant, failure-isolated) ──────────────
            ai_score = None
            if _ai_engine is not None:
                try:
                    ai_dna_score = None
                    ai_protein_score = None
                    ref_seq = variant.get("ref_sequence")
                    alt_seq = variant.get("alt_sequence")
                    wt_aa = variant.get("wildtype_aa")
                    mt_aa = variant.get("mutant_aa")
                    if ref_seq and alt_seq:
                        logger.info(
                            "[PIPELINE:VARIANT] %s:%s %s>%s -> calling AI:DNABERT-2",
                            chrom,
                            pos_v,
                            ref_v,
                            alt_v,
                        )
                        ai_dna_score = _ai_engine.score_dna(ref_seq, alt_seq)
                    if wt_aa and mt_aa:
                        logger.info(
                            "[PIPELINE:VARIANT] %s:%s %s>%s -> calling AI:ESM-2",
                            chrom,
                            pos_v,
                            ref_v,
                            alt_v,
                        )
                        ai_protein_score = _ai_engine.score_protein(wt_aa, mt_aa)
                    ai_score = _ai_engine.combined_score(ai_dna_score, ai_protein_score)
                except Exception as _ai_exc:
                    logger.debug("AI scoring failed for variant %s:%s: %s", chrom, pos_v, _ai_exc)
                    ai_score = None

            comp_score = EvidenceAggregator.compute_computational_score(
                cadd_phred=variant.get("cadd_phred"),
                revel_score=variant.get("revel_score"),
                spliceai_score=variant.get("spliceai_score"),
                alphamissense_score=variant.get("alphamissense_score"),
            )
            cv_score = None
            if cv_hit:
                cv_score = EvidenceAggregator.clinvar_sig_to_score(
                    cv_hit.significance, cv_hit.review_stars
                )

            ev_res = evidence_agg.aggregate_from_acmg(
                acmg_result=acmg_res,
                clinvar_score=cv_score,
                computational_score=comp_score,
                ai_score=ai_score,
            )

            acmg_batch.append(
                {
                    **acmg_res.to_dict(),
                    **ev_res.to_dict(),
                    **_availability_fields(
                        gene=gene,
                        gene_unavailable_reason=gene_unavailable_reason,
                        clinvar_enabled=clinvar_lkp.enabled,
                        clinvar_significance=clinvar_significance,
                        gnomad_enabled=gnomad_lkp.enabled,
                        gnomad_af=gnomad_af,
                        gnomad_af_popmax=gnomad_af_popmax,
                        gnomad_af_absent=gnomad_af_absent,
                    ),
                }
            )
            logger.info(
                "[PIPELINE:VARIANT] END idx=%d/%d %s:%s %s>%s elapsed=%.2fs",
                _variant_idx,
                _total_variants,
                chrom,
                pos_v,
                ref_v,
                alt_v,
                time.monotonic() - _variant_t0,
            )

        except Exception as _var_exc:
            logger.error(
                "[%s] ACMG classify failed for %s:%d %s>%s: %s",
                sample_id,
                chrom,
                pos_v,
                ref_v,
                alt_v,
                _var_exc,
            )
            acmg_batch.append(
                {
                    "chrom": chrom,
                    "pos": pos_v,
                    "ref": ref_v,
                    "alt": alt_v,
                    "gene": gene or None,
                    "gene_unavailable_reason": None if gene else gene_unavailable_reason,
                    "error": str(_var_exc),
                }
            )
            logger.info(
                "[PIPELINE:VARIANT] END (error) idx=%d/%d %s:%s %s>%s elapsed=%.2fs",
                _variant_idx,
                _total_variants,
                chrom,
                pos_v,
                ref_v,
                alt_v,
                time.monotonic() - _variant_t0,
            )

    return acmg_batch
