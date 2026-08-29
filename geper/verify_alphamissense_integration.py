"""
Verification script for the AlphaMissense integration.

WHY THIS EXISTS / WHAT IT DOES AND DOES NOT PROVE
----------------------------------------------------
Same sandbox network constraint documented in dry_run_harness.py
applies here: this environment cannot reach huggingface.co (real
HyenaDNA/Nucleotide-Transformer/RNA-FM/ESM-2 weights),
rest.ensembl.org, eutils.ncbi.nlm.nih.gov, or NCBI BLAST. Those stages
are therefore exercised with the same lightweight fakes
dry_run_harness.py already uses for the *existing* pipeline, at the
`_load_impl` / `_infer_impl` boundary -- proving the plumbing, wiring,
and error-handling paths are correct without requiring weights this
sandbox cannot download.

AlphaMissense is different and is NOT faked here: `tabix` (htslib) was
installed in this sandbox (`apt-get install tabix`, an allowed
package-registry domain) and a real, valid bgzip'd + tabix-indexed toy
catalogue was built locally (see testdata/alphamissense_fixture/) with the
exact real schema AlphaMissense_hg19.tsv.gz ships
(#CHROM POS REF ALT genome uniprot_id transcript_id protein_variant
am_pathogenicity am_class) and entries at the exact genomic
coordinates of the three known ClinVar missense variants already
vetted elsewhere in this repo (testdata/known_variants_grch37.vcf --
F5 rs6025 / Factor V Leiden, HBB rs334 / sickle-cell HbS, ALDH2 rs671).
So PART 2 and PART 3 below exercise the REAL, unmodified
AlphaMissenseModel code end-to-end -- real subprocess call to a real
`tabix` binary against a real bgzip+tabix index, real TSV parsing --
against a local file that stands in only for the network-hosted
Google Cloud Storage catalogue (which this sandbox cannot reach), via
the documented GEPER_ALPHAMISSENSE_HG19_LOCAL override.

This is not a substitute for running the pipeline with a real network
connection to the actual AlphaMissense catalogue; see README.md's
"AlphaMissense" section for what to run yourself to confirm that.
"""

import os as _os
import shutil
import sys
from unittest import mock

# Resolve relative to THIS FILE, not the process's current working
# directory. `sys.path.insert(0, ".")` only works when the script
# happens to be launched with cwd == the geper/ project root; in a
# Colab cell (`!python /content/geper/verify_alphamissense_integration.py`
# run from /content, or the file executed via `%run`/`exec` rather than
# `python <path>`) "." can resolve to a completely different directory,
# so `from models import MODEL_REGISTRY` below fails with a confusing
# ModuleNotFoundError. Inserting the script's own absolute directory
# works regardless of how/where it's invoked from.
_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Sandbox robustness only (see _fake_heavy_deps.py's own docstring): if
# this environment's local torch/transformers install is missing or
# broken, stub them so `from models import MODEL_REGISTRY` below (which
# imports every model, including the PyTorch-based ones) doesn't fail
# before this script even gets to AlphaMissense's own, real, unmocked
# code path. A no-op when a real, working torch is already importable.
import _fake_heavy_deps  # noqa: E402

_fake_heavy_deps.install()

AM_FIXTURE_HG19 = _os.path.join(
    _SCRIPT_DIR, "testdata", "alphamissense_fixture", "AlphaMissense_hg19_test.final.tsv.gz"
)
# Startup validation's dummy key has no assembly context (it runs
# before any VCF is parsed), so it defaults to hg38 (see
# resolve_genome_label(None) in models/alphamissense.py) -- point the
# hg38 slot at the same local fixture too, purely so this sandboxed
# test never needs the real (network-blocked) GCS hg38 file. The
# fixture's content is genome-agnostic for plumbing-testing purposes;
# this does NOT imply hg19==hg38 coordinates in real biology.
AM_FIXTURE_HG38 = AM_FIXTURE_HG19


def _fake_load_impl(self):
    self.tokenizer = "fake-tokenizer"
    self.model = "fake-model"


def _fake_infer_impl(self, sequence, **kwargs):
    return {
        "embedding_mean": [0.0] * 8,
        "embedding_dim": 8,
        "num_tokens": max(1, len(sequence) // 4),
    }


def _fake_verify_materialized(self):
    return None


def main() -> int:
    all_passed = True

    # ==================================================================
    print("=" * 78)
    print("PART 1 -- Unit-level: SequenceRouter.is_missense_eligible")
    print("=" * 78)
    import json as _json
    from pipeline.router import SequenceRouter
    from pipeline.vcf_parser import Variant

    router = SequenceRouter()

    def _variant(chrom="1", pos=100, ref="A", alt="T", variant_type="SNV", info=None):
        return Variant(
            chrom=chrom,
            pos=pos,
            variant_id=".",
            ref=ref,
            alt=alt,
            qual=None,
            filter_status=".",
            info=info or {},
        )

    # Real, live-fetched TP53/BRCA1 transcript fixtures (same ones
    # tests/test_bp1_bp3_bp6_bp7.py and tests/test_is_missense_eligible.py
    # validate `protein_effect_flags` against) -- eligibility is now
    # decided from `transcript_result`'s transcript-CDS-frame
    # classification, not from ESM-2's local-window ref/alt protein
    # strings, so these fixtures (not raw protein strings) are the
    # correct input here.
    _fixtures_dir = _os.path.join(_SCRIPT_DIR, "tests", "fixtures")
    with open(_os.path.join(_fixtures_dir, "ps1_pm5_transcript.json"), "r", encoding="utf-8") as _fh:
        _tp53_record = _json.load(_fh)["transcript"]
    with open(_os.path.join(_fixtures_dir, "pvs1_transcripts.json"), "r", encoding="utf-8") as _fh:
        _pvs1_transcripts = _json.load(_fh)["transcripts"]

    def _tp53_result():
        return {"skipped": False, "found": True, "transcript": _tp53_record}

    def _brca1_result():
        return {"skipped": False, "found": True, "transcript": _pvs1_transcripts["BRCA1"]}

    cases = [
        # (label, variant, transcript_result, expected_eligible)
        # Real: TP53 p.Arg248Trp, c.742C>T, 17:7674221 G>A -- ClinVar
        # Pathogenic, expert panel. codon 248, R->W.
        ("clean missense (real TP53 R248W)", _variant("17", 7674221, "G", "A"), _tp53_result(), True),
        # Real: TP53 p.Arg175=, c.525C>T, 17:7675087 G>A -- codon 175, R->R.
        ("synonymous (real TP53 R175=)", _variant("17", 7675087, "G", "A"), _tp53_result(), False),
        # Real: BRCA1 codon 50, K->* (nonsense).
        ("nonsense (real BRCA1 codon 50 K->*)", _variant("17", 43106520, "T", "A"), _brca1_result(), False),
        # Real: TP53 c.792_794del (p.Leu265del), in-frame deletion.
        (
            "in-frame indel (real TP53 c.792_794del)",
            _variant("17", 7673826, "AGTAG", "AG", variant_type="deletion"),
            _tp53_result(),
            False,
        ),
        # Real: frameshift deletion at the same TP53 locus.
        (
            "frameshift (length changes)",
            _variant("17", 7673826, "AG", "A", variant_type="deletion"),
            _tp53_result(),
            False,
        ),
        ("undetermined consequence (no transcript data)", _variant("17", 7674221, "G", "A"), None, False),
        (
            "non-SNV variant type (insertion)",
            _variant(ref="A", alt="ATG", variant_type="insertion"),
            _tp53_result(),
            False,
        ),
        ("symbolic ALT allele", _variant(alt="<DEL>"), _tp53_result(), False),
        ("SVTYPE-flagged record", _variant(info={"SVTYPE": "DEL"}), _tp53_result(), False),
    ]

    part1_ok = True
    for label, variant, transcript_result, expected in cases:
        actual = router.is_missense_eligible(variant, transcript_result)
        status = "PASS" if actual == expected else "FAIL"
        if actual != expected:
            part1_ok = False
        print(f"  [{status}] {label}: expected={expected}, got={actual}")

    if part1_ok:
        print(
            "\nPART 1: PASSED -- missense eligibility, classified from real "
            "transcript-CDS-frame data, correctly includes only clean, "
            "single-residue SNV substitutions and excludes synonymous, "
            "nonsense, frameshift/in-frame-indel, symbolic/structural, "
            "non-SNV, and undetermined-consequence cases.\n"
        )
    else:
        print("\nPART 1: FAILED\n")
        all_passed = False

    # ==================================================================
    print("=" * 78)
    print("PART 2 -- Unit-level: AlphaMissenseModel against a REAL tabix catalogue")
    print("=" * 78)

    if shutil.which("tabix") is None:
        print("  SKIPPED: 'tabix' is not installed in this environment.")
    else:
        from models.alphamissense import AlphaMissenseModel
        from utils.model_cache import ModelCache

        # Isolation note: ModelCache is a process-wide singleton by
        # design (see utils/model_cache.py) -- correct and intentional
        # for a real pipeline run, where exactly one instance of each
        # model class is ever constructed per process (see
        # orchestrator.py's `_model_instances` dict). This script runs
        # multiple independent test "parts" in one process, so it
        # explicitly clears the cache between parts to mirror a fresh
        # process each time, rather than to work around anything wrong
        # with AlphaMissenseModel itself.
        ModelCache.clear()

        with mock.patch.dict(
            "os.environ",
            {"GEPER_ALPHAMISSENSE_HG19_LOCAL": AM_FIXTURE_HG19, "GEPER_ALPHAMISSENSE_HG38_LOCAL": AM_FIXTURE_HG38},
        ):
            # config.py reads os.environ at import/dataclass-default time,
            # so rebuild CONFIG.alphamissense with the env override applied
            # for this test, exactly as a real deployment setting the
            # env var before starting the process would experience.
            import importlib
            import config as config_module

            importlib.reload(config_module)
            from config import CONFIG as RELOADED_CONFIG

            import models.alphamissense as am_module

            am_module.CONFIG = RELOADED_CONFIG

            part2_ok = True
            try:
                model = AlphaMissenseModel()

                available = AlphaMissenseModel.is_available()
                print(f"  is_available(): {available}")
                part2_ok = part2_ok and available

                # A real hit -- F5 rs6025 (Factor V Leiden), GRCh37.
                hit = model.predict("1:169519049:T:C", assembly="GRCh37")
                hit_ok = hit.get("found") is True and hit.get("am_class") == "likely_pathogenic"
                print(
                    f"  known missense hit (F5 rs6025): found={hit.get('found')}, "
                    f"am_class={hit.get('am_class')}, "
                    f"am_pathogenicity={hit.get('am_pathogenicity')} "
                    f"-> {'PASS' if hit_ok else 'FAIL'}"
                )
                part2_ok = part2_ok and hit_ok

                # A real miss -- a position not in the catalogue.
                miss = model.predict("1:999999999:A:T", assembly="GRCh37")
                miss_ok = miss.get("found") is False
                print(f"  no-catalogue-entry lookup: found={miss.get('found')} -> {'PASS' if miss_ok else 'FAIL'}")
                part2_ok = part2_ok and miss_ok

                # Startup-validation dummy key round-trips without raising.
                dummy = model.predict("1:100000:A:T", assembly="GRCh37")
                dummy_ok = "found" in dummy
                print(f"  startup dummy-key round-trip: {'PASS' if dummy_ok else 'FAIL'}")
                part2_ok = part2_ok and dummy_ok

                # Malformed lookup key raises ModelInferenceError, not a
                # bare/uncaught exception -- keeps the orchestrator's
                # graceful-degradation contract intact.
                from utils.exceptions import ModelInferenceError

                try:
                    model.predict("not-a-valid-key")
                    print("  malformed key -> FAIL (no exception raised)")
                    part2_ok = False
                except ModelInferenceError:
                    print("  malformed key -> PASS (raised ModelInferenceError)")

                # Device/precision reporting for the startup validation table.
                device_ok = str(model.device) == "cpu"
                precision = model._report_precision()
                precision_ok = "n/a" in precision.lower()
                print(
                    f"  device={model.device} ({'PASS' if device_ok else 'FAIL'}), "
                    f"precision='{precision}' ({'PASS' if precision_ok else 'FAIL'})"
                )
                part2_ok = part2_ok and device_ok and precision_ok

            except Exception as exc:  # noqa: BLE001
                print(f"  PART 2 raised an unexpected exception: {exc}")
                part2_ok = False

        if part2_ok:
            print(
                "\nPART 2: PASSED -- real tabix subprocess queries against a real "
                "bgzip+tabix-indexed catalogue file resolve correctly, gracefully "
                "report not-found, and translate malformed input into "
                "ModelInferenceError.\n"
            )
        else:
            print("\nPART 2: FAILED\n")
            all_passed = False

    # ==================================================================
    print("=" * 78)
    print(
        "PART 3 -- Integration: full orchestrator run, existing stages "
        "faked (network-gated, per dry_run_harness.py), AlphaMissense REAL"
    )
    print("=" * 78)

    from models import MODEL_REGISTRY
    from models.base_model import BaseGenomicModel
    from models.alphamissense import AlphaMissenseModel
    from pipeline.sequence_context import SequenceContext, SequenceContextGenerator
    from database.blast_client import BLASTClient
    from database.clinvar_client import ClinVarClient
    from database.dbsnp_client import DbSNPClient
    from utils.model_cache import ModelCache

    ModelCache.clear()  # fresh-process isolation -- see the note in PART 2.

    patches = []
    for key, model_cls in MODEL_REGISTRY.items():
        if key == "alphamissense":
            continue  # left real -- this is what we're verifying
        patches.append(mock.patch.object(model_cls, "_load_impl", _fake_load_impl))
        patches.append(mock.patch.object(model_cls, "_infer_impl", _fake_infer_impl))
    patches.append(mock.patch.object(BaseGenomicModel, "_verify_materialized", _fake_verify_materialized))

    # Build a context with a REAL, deterministic single-residue missense
    # substitution at the variant position, for every variant in the
    # test VCF -- unlike dry_run_harness.py's fixture (ref==alt, always
    # synonymous), this one actually exercises AlphaMissense routing.
    # ATG + AAA (Lys) ... one base of the second codon is flipped by the
    # variant's REF>ALT, changing AAA (Lys) -> AAC (Asn): a clean
    # missense, same length, no stop introduced.
    def _fake_build_context(self, variant, flank_size=None):
        flank = flank_size if flank_size is not None else 500
        ref_seq = "ATG" + "AAA" + "CCC" * 100
        alt_seq = "ATG" + "AAC" + "CCC" * 100
        return SequenceContext(
            chrom=variant.chrom,
            window_start=max(1, variant.pos - flank),
            window_end=variant.pos + flank,
            flank_size=flank,
            ref_sequence=ref_seq,
            alt_sequence=alt_seq,
            variant_offset=flank,
        )

    patches.append(mock.patch.object(SequenceContextGenerator, "build_context", _fake_build_context))
    patches.append(
        mock.patch.object(
            BLASTClient,
            "_search_remote",
            lambda self, seq, program, database, max_hits: {
                "mode": "remote",
                "database": database,
                "hits": [],
                "hit_count": 0,
            },
        )
    )

    # ClinVar: return a real "found" record for the F5 Leiden coordinate
    # to prove ClinVar plumbing is completely untouched and still works
    # end-to-end alongside the new AlphaMissense stage.
    def _fake_clinvar_query(self, variant, rsid=None, assembly=None):
        if variant.chrom == "1" and variant.pos == 169519049:
            return {
                "query": f"{variant.chrom}:{variant.pos}",
                "found": True,
                "records": [
                    {
                        "clinical_significance": "Pathogenic",
                        "review_status": "reviewed by expert panel",
                        "condition": ["Factor V Leiden thrombophilia"],
                    }
                ],
            }
        return {"query": None, "found": False}

    patches.append(mock.patch.object(ClinVarClient, "query_variant", _fake_clinvar_query))
    patches.append(
        mock.patch.object(
            DbSNPClient,
            "lookup_variant",
            lambda self, variant, assembly=None: {"rsid": None, "found": False},
        )
    )

    # Transcript structure: `_run_alphamissense_stage` now decides
    # missense eligibility from `transcript_result` (the same
    # transcript-CDS-frame `protein_effect_flags` machinery BP7/BP1
    # use), not from ESM-2's local-window ref/alt protein strings --
    # see `pipeline/router.py::SequenceRouter.is_missense_eligible`'s
    # docstring. `TranscriptLookup` itself calls the network-gated
    # Ensembl REST API (unreachable in this sandbox, per the module
    # docstring), so it is faked here like the other network-bound
    # providers above. A minimal single-exon, single-codon synthetic
    # CDS per variant is enough: it needs to classify each of the three
    # known variants as a real, clean missense substitution (not
    # synonymous/nonsense/frameshift) at its own genomic position --
    # matching the same real REF>ALT codon changes described in
    # testdata/known_variants_grch37.vcf (F5 rs6025 T>C, HBB rs334
    # T>A, ALDH2 rs671 G>A), not standing in for the real biology at
    # those loci (which the real, unmocked AlphaMissense lookup below
    # is what actually gets verified against real catalogue data).
    _fake_codons = {
        ("1", 169519049): "TTT",  # F5 rs6025: T>C -> CTT (Phe->Leu), missense
        ("11", 5248232): "TTT",  # HBB rs334: T>A -> ATT (Phe->Ile), missense
        ("12", 112241766): "GGG",  # ALDH2 rs671: G>A -> AGG (Gly->Arg), missense
    }

    def _fake_transcript_query(self, variant, assembly="GRCh38", gene_symbol=None):
        codon = _fake_codons.get((variant.chrom, variant.pos))
        if codon is None:
            return {"found": False, "skipped": False, "transcript": None}
        transcript = {
            "transcript_id": "FAKE0000001",
            "gene_symbol": gene_symbol or "FAKE",
            "chrom": variant.chrom,
            "strand": 1,
            "cds_genomic_start": variant.pos,
            "cds_genomic_end": variant.pos + 2,
            "is_mane_select": True,
            "is_canonical": True,
            "source": "verify_alphamissense_integration fake",
            "cds_sequence": codon,
            "exons": [{"start": variant.pos, "end": variant.pos + 2}],
        }
        return {"found": True, "skipped": False, "transcript": transcript}

    from pipeline.pvs1.lookup import TranscriptLookup

    patches.append(mock.patch.object(TranscriptLookup, "query_variant", _fake_transcript_query))

    with mock.patch.dict(
        "os.environ",
        {"GEPER_ALPHAMISSENSE_HG19_LOCAL": AM_FIXTURE_HG19, "GEPER_ALPHAMISSENSE_HG38_LOCAL": AM_FIXTURE_HG38},
    ):
        import importlib
        import config as config_module

        importlib.reload(config_module)
        from config import CONFIG as RELOADED_CONFIG
        import models.alphamissense as am_module

        am_module.CONFIG = RELOADED_CONFIG

        captured_startup_rows: list = []

        def _capture_startup_rows(rows):
            captured_startup_rows.extend(rows)

        for p in patches:
            p.start()
        part3_ok = True
        try:
            import importlib as _il
            import pipeline.orchestrator as orch_module

            _il.reload(orch_module)

            startup_patch = mock.patch.object(
                orch_module.GeperPipeline,
                "_log_startup_report",
                staticmethod(_capture_startup_rows),
            )
            startup_patch.start()

            pipeline = orch_module.GeperPipeline(
                output_dir=_os.path.join(_SCRIPT_DIR, "_verify_alphamissense_output"),
            )
            json_document = pipeline.run(
                vcf_path="testdata/known_variants_grch37.vcf",
                resume=False,
            )
            startup_patch.stop()

            row_by_name = {r["name"]: r for r in captured_startup_rows}

            # --- (a) HyenaDNA still loads (startup validation PASS) ---
            # (Previously checked DNABERT2; DNABERT-2 has been removed
            # from GEPER entirely -- see LICENSE_AUDIT.md. HyenaDNA is
            # now the universal default DNA model, so it is the
            # equivalent "does the always-on default model still load"
            # smoke test.)
            hyenadna_ok = row_by_name.get("HyenaDNA", {}).get("status") == "PASS"
            print(
                f"  (a) HyenaDNA startup status: "
                f"{row_by_name.get('HyenaDNA', {}).get('status')} "
                f"-> {'PASS' if hyenadna_ok else 'FAIL'}"
            )
            part3_ok = part3_ok and hyenadna_ok

            # --- (b) AlphaMissense appears in startup validation table
            #         with Device / Precision / Status ---
            am_row = row_by_name.get("AlphaMissense")
            am_row_ok = bool(
                am_row
                and am_row.get("status") == "PASS"
                and am_row.get("device") == "cpu"
                and "n/a" in am_row.get("precision", "").lower()
            )
            print(f"  (b) AlphaMissense startup row: {am_row} -> {'PASS' if am_row_ok else 'FAIL'}")
            part3_ok = part3_ok and am_row_ok

            # --- (c) ClinVar still returns records ---
            variants = json_document.get("variants", [])
            f5_result = next(
                (v for v in variants if v["variant"]["chrom"] == "1" and v["variant"]["pos"] == 169519049),
                None,
            )
            clinvar_ok = bool(
                f5_result
                and f5_result["clinvar"].get("found")
                and f5_result["clinvar"]["records"][0]["clinical_significance"] == "Pathogenic"
            )
            print(f"  (c) ClinVar record present for F5 rs6025: {'PASS' if clinvar_ok else 'FAIL'}")
            part3_ok = part3_ok and clinvar_ok

            # --- (d) AlphaMissense returns a real prediction for the
            #         eligible missense variant ---
            am_result = f5_result["alphamissense"] if f5_result else {}
            am_ok = bool(
                am_result.get("found") is True
                and am_result.get("am_class") == "likely_pathogenic"
                and not am_result.get("skipped")
            )
            print(f"  (d) AlphaMissense prediction for F5 rs6025: {am_result} -> {'PASS' if am_ok else 'FAIL'}")
            part3_ok = part3_ok and am_ok

            # --- (e) No regressions in the other two known variants
            #         (HBB / ALDH2) -- also missense-eligible, also hit
            #         the fixture catalogue ---
            other_hits = [
                v for v in variants if v["variant"]["chrom"] in ("11", "12") and v["alphamissense"].get("found")
            ]
            others_ok = len(other_hits) == 2
            print(f"  (e) HBB + ALDH2 AlphaMissense hits: {len(other_hits)}/2 -> {'PASS' if others_ok else 'FAIL'}")
            part3_ok = part3_ok and others_ok

            # --- (f) JSON output contains the alphamissense key ---
            json_ok = all("alphamissense" in v for v in variants)
            print(f"  (f) 'alphamissense' present in every JSON variant record: {'PASS' if json_ok else 'FAIL'}")
            part3_ok = part3_ok and json_ok

            # --- (g) Markdown report contains an AlphaMissense section ---
            from report.report_generator import ReportGenerator

            md = ReportGenerator().generate(json_document)
            md_ok = "### AlphaMissense" in md and "am_pathogenicity" in md
            print(f"  (g) Markdown report includes '### AlphaMissense' section: {'PASS' if md_ok else 'FAIL'}")
            part3_ok = part3_ok and md_ok

            # --- (h) No unexpected stage errors recorded for the 3
            #         known variants ---
            no_errors = all(not v.get("errors") for v in variants)
            print(
                f"  (h) No stage errors recorded for any known variant: "
                f"{'PASS' if no_errors else 'FAIL'}"
                + ("" if no_errors else f" ({[v.get('errors') for v in variants]})")
            )
            part3_ok = part3_ok and no_errors

        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            print(f"  PART 3 raised an unexpected exception: {exc}")
            part3_ok = False
        finally:
            for p in patches:
                p.stop()

    if part3_ok:
        print(
            "\nPART 3: PASSED -- full pipeline run succeeds with AlphaMissense "
            "integrated: existing stages (HyenaDNA, ClinVar, dbSNP, RNA-FM, ESM-2, "
            "BLAST, reporting, startup validation) are unaffected, and "
            "AlphaMissense correctly routes only eligible missense variants, "
            "returns predictions, and appears in both JSON and Markdown output.\n"
        )
    else:
        print("\nPART 3: FAILED\n")
        all_passed = False

    # ==================================================================
    print("=" * 78)
    print("PART 4 -- Configuration: enable/disable + graceful missing-tabix")
    print("=" * 78)
    from models.alphamissense import AlphaMissenseModel as AMModel2
    from utils.model_cache import ModelCache as ModelCache2

    ModelCache2.clear()

    with mock.patch.dict("os.environ", {"GEPER_ENABLE_ALPHAMISSENSE": "false"}):
        import importlib
        import config as config_module

        importlib.reload(config_module)
        import models.alphamissense as am_module

        am_module.CONFIG = config_module.CONFIG
        disabled_ok = AMModel2.is_available() is False
        print(
            f"  GEPER_ENABLE_ALPHAMISSENSE=false -> is_available()="
            f"{AMModel2.is_available()} -> {'PASS' if disabled_ok else 'FAIL'}"
        )

    with mock.patch("shutil.which", return_value=None):
        import importlib
        import config as config_module

        importlib.reload(config_module)
        import models.alphamissense as am_module

        am_module.CONFIG = config_module.CONFIG
        missing_tabix_ok = AMModel2.is_available() is False
        print(
            f"  tabix not on PATH -> is_available()={AMModel2.is_available()} "
            f"-> {'PASS' if missing_tabix_ok else 'FAIL'}"
        )

    # restore real config for anything importing this module afterwards
    import importlib
    import config as config_module

    importlib.reload(config_module)
    import models.alphamissense as am_module

    am_module.CONFIG = config_module.CONFIG

    part4_ok = disabled_ok and missing_tabix_ok
    if part4_ok:
        print(
            "\nPART 4: PASSED -- AlphaMissense can be disabled via config, and "
            "degrades to 'unavailable' (never a crash) when tabix is missing.\n"
        )
    else:
        print("\nPART 4: FAILED\n")
        all_passed = False

    # ==================================================================
    print("=" * 78)
    print("OVERALL:", "ALL PARTS PASSED" if all_passed else "ONE OR MORE PARTS FAILED")
    print("=" * 78)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
