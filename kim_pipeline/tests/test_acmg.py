"""
tests/test_acmg.py
──────────────────
Unit tests for pipeline.acmg.classifier.AcmgClassifier and VariantEvidence.
"""

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence, AcmgResult


# ── Helpers ───────────────────────────────────────────────────────────────────


def _clf(cfg: dict | None = None) -> AcmgClassifier:
    return AcmgClassifier(cfg=cfg or {})


def _classify(evidence: VariantEvidence, cfg: dict | None = None) -> AcmgResult:
    return _clf(cfg).classify(evidence)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_pathogenic_pvs1_ps1():
    """PVS1 + PS1 → Pathogenic."""
    ev = VariantEvidence(
        chrom="17",
        pos=43057051,
        ref="A",
        alt="T",
        gene="BRCA1",
        is_lof=True,
        lof_gene_intolerant=True,
        same_aa_pathogenic=True,
    )
    result = _classify(ev)
    assert result.classification == "Pathogenic"
    assert "PVS1" in result.criteria_met
    assert "PS1" in result.criteria_met


def test_likely_pathogenic_pm2_pp3_pp5():
    """Low AF + damaging in-silico + ClinVar Pathogenic + hotspot → LP or P."""
    ev = VariantEvidence(
        chrom="17",
        pos=43057051,
        ref="A",
        alt="T",
        gene="BRCA1",
        gnomad_af=0.000001,
        cadd_phred=35.0,
        revel_score=0.9,
        clinvar_significance="Pathogenic",
        clinvar_stars=2,
        clinvar_conflicting=False,
        in_hotspot=True,  # triggers PM1 → now PM1+PM2+PP3+PP5 → P/LP
        is_missense=True,
    )
    result = _classify(ev)
    assert result.classification in {"Likely_Pathogenic", "Pathogenic"}
    assert "PM2" in result.criteria_met
    assert "PP3" in result.criteria_met
    assert "PP5" in result.criteria_met


def test_benign_ba1():
    """Very high population AF (≥ BA1 threshold) → Benign."""
    ev = VariantEvidence(
        chrom="1",
        pos=100000,
        ref="A",
        alt="G",
        gnomad_af=0.10,
    )
    result = _classify(ev)
    assert result.classification == "Benign"
    assert "BA1" in result.criteria_met


def test_likely_benign_bp4():
    """All in-silico predictors benign + BS1 (high AF) → BP4+BS1 → Likely_Benign or Benign."""
    ev = VariantEvidence(
        chrom="2",
        pos=200000,
        ref="C",
        alt="T",
        # AF above PM2 threshold (0.0001) so PM2 not triggered; below BA1 (0.05)
        gnomad_af=0.01,
        gnomad_af_popmax=0.01,
        cadd_phred=5.0,
        revel_score=0.05,
        spliceai_score=0.02,
    )
    result = _classify(ev)
    assert result.classification in {"Likely_Benign", "Benign"}
    assert "BP4" in result.criteria_met


def test_vus_no_evidence():
    """No evidence fields set → Uncertain_Significance."""
    ev = VariantEvidence(
        chrom="3",
        pos=300000,
        ref="G",
        alt="A",
    )
    result = _classify(ev)
    assert result.classification == "Uncertain_Significance"


def test_score_is_normalised():
    """Score is always in [0.0, 1.0] regardless of input."""
    test_cases = [
        VariantEvidence(gnomad_af=0.10),  # Benign
        VariantEvidence(is_lof=True, lof_gene_intolerant=True, same_aa_pathogenic=True),  # Path
        VariantEvidence(),  # VUS
        VariantEvidence(
            cadd_phred=50.0, revel_score=0.99, clinvar_significance="Pathogenic", clinvar_stars=3
        ),
    ]
    clf = _clf()
    for ev in test_cases:
        result = clf.classify(ev)
        assert 0.0 <= result.score <= 1.0, f"score={result.score} out of [0,1] for {ev}"


def test_pp5_requires_no_conflicts():
    """PP5 must NOT be met when clinvar_conflicting=True."""
    ev = VariantEvidence(
        clinvar_significance="Pathogenic",
        clinvar_stars=2,
        clinvar_conflicting=True,
    )
    result = _classify(ev)
    assert "PP5" not in result.criteria_met


def test_pp5_requires_stars_ge_1():
    """PP5 requires at least 1 review star."""
    ev = VariantEvidence(
        clinvar_significance="Pathogenic",
        clinvar_stars=0,
        clinvar_conflicting=False,
    )
    result = _classify(ev)
    assert "PP5" not in result.criteria_met


def test_ba1_threshold_configurable():
    """BA1 threshold can be overridden via cfg['acmg_thresholds']['ba1_af']."""
    ev = VariantEvidence(gnomad_af=0.02)
    # Default ba1_af=0.05 → not BA1
    default_result = _classify(ev)
    assert "BA1" not in default_result.criteria_met

    # Custom ba1_af=0.01 → 0.02 ≥ 0.01 → BA1 met
    cfg = {"acmg_thresholds": {"ba1_af": 0.01}}
    custom_result = _classify(ev, cfg=cfg)
    assert "BA1" in custom_result.criteria_met
    assert custom_result.classification == "Benign"


def test_pm2_absent_from_gnomad():
    """If gnomad_af is None AND gnomad_af_absent=True (confirmed absent), PM2 is met.
    If gnomad_af_absent is False/None (lookup unavailable), PM2 must NOT be met.
    """
    ev_absent = VariantEvidence(gnomad_af=None, gnomad_af_popmax=None, gnomad_af_absent=True)
    result_absent = _classify(ev_absent)
    assert "PM2" in result_absent.criteria_met, "PM2 should fire when absence is confirmed"

    ev_unavailable = VariantEvidence(gnomad_af=None, gnomad_af_popmax=None, gnomad_af_absent=False)
    result_unavail = _classify(ev_unavailable)
    assert "PM2" not in result_unavail.criteria_met, "PM2 must not fire when lookup was unavailable"


def test_pm2_not_met_above_threshold():
    """gnomad_af above PM2 threshold → PM2 not met."""
    ev = VariantEvidence(gnomad_af=0.01)  # default pm2_af_max=0.0001
    result = _classify(ev)
    assert "PM2" not in result.criteria_met


def test_pvs1_requires_lof_intolerant():
    """PVS1 requires BOTH is_lof AND lof_gene_intolerant."""
    ev_lof_only = VariantEvidence(is_lof=True, lof_gene_intolerant=False)
    ev_intol_only = VariantEvidence(is_lof=False, lof_gene_intolerant=True)
    clf = _clf()
    assert "PVS1" not in clf.classify(ev_lof_only).criteria_met
    assert "PVS1" not in clf.classify(ev_intol_only).criteria_met


def test_result_has_expected_fields():
    """AcmgResult has all required fields populated after classify()."""
    ev = VariantEvidence(chrom="17", pos=1000, ref="A", alt="T", gene="BRCA1")
    result = _classify(ev)
    assert isinstance(result.classification, str)
    assert isinstance(result.score, float)
    assert isinstance(result.criteria_met, list)
    assert isinstance(result.criteria_not_met, list)
    assert result.chrom == "17"
    assert result.gene == "BRCA1"
