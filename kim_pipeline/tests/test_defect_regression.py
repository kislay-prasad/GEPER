"""
tests/test_defect_regression.py
───────────────────────────────
Regression tests for GEPER v12 defect fixes (Defects 1–15).

Coverage matrix
───────────────
D1  – 5-tuple unpack from get_codon_and_aa()
D2  – bcftools norm integration (real command captured via subprocess interception;
      test_bcftools_available is the one real-binary smoke test, tool may not be installed)
D3  – HGVS generation: c./n./g. prefix correctness
D4  – GFF3 phase handling in codon frame calculation
D5  – Allele balance uses correct ALT index for multiallelic VCFs
D6  – PGx subset allele exclusion (deterministic diplotype)
D7  – BP7 fires from synonymous_or_intronic
D8  – BP1 wired to ClinVar reputable-source benign
D9  – Duplicate @staticmethod removed from EvidenceAggregator
D10 – Version: one source (component_identity.VERSION), read by every surface
D11 – PS1/PM5 mutual exclusion
D12 – BLAST results attached to annotation output
D13 – VEP HGVSc/HGVSp propagated into AnnotatedVariant
D14 – ClinVar lookup() cache_key bug fixed; check_same_codon_pathogenic cached
D15 – PP2 uses missense constraint (oe_mis/mis_z), not LoF intolerance
"""

from __future__ import annotations

from typing import Dict
from unittest.mock import MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# D3 – HGVS generation
# ══════════════════════════════════════════════════════════════════════════════


class TestHgvsGeneration:
    """Defect 3: HGVS must never produce NM_xxx:g. for transcript references,
    AND must never pair a c./n. prefix with an unverified raw genomic
    position (e.g. NM_xxxxx:c.94781858G>A) — both are invalid HGVS.

    The previous version of this test class only checked the prefix
    letter (c. vs g.) and asserted the raw genomic position was an
    acceptable c. coordinate. It was not: without a verified CDS-relative
    coordinate, _build_hgvs must fall back to genomic (g.) notation.
    """

    def _hgvs(
        self, chrom, pos, ref, alt, transcript=None, cds_pos=None, end_cds_pos=None, strand=None
    ):
        from pipeline.annotation.stage import _build_hgvs

        return _build_hgvs(
            chrom,
            pos,
            ref,
            alt,
            transcript,
            cds_pos=cds_pos,
            end_cds_pos=end_cds_pos,
            strand=strand,
        )

    def test_no_transcript_uses_genomic_g(self):
        h = self._hgvs("chr17", 43057051, "A", "T", None)
        assert ":g." in h
        assert "chr17" in h

    def test_nm_transcript_without_cds_pos_falls_back_to_genomic(self):
        # No verified CDS-relative coordinate supplied → must NOT emit
        # NM_007294.4:c.43057051A>T (invalid: that's a genomic position).
        h = self._hgvs("chr17", 43057051, "A", "T", "NM_007294.4")
        assert ":g." in h
        assert ":c." not in h

    def test_nm_transcript_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr17", 43057051, "A", "T", "NM_007294.4", cds_pos=181, strand="+")
        assert h == "NM_007294.4:c.181A>T"

    def test_xm_transcript_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr1", 100, "G", "A", "XM_001234.1", cds_pos=10, strand="+")
        assert h.startswith("XM_001234.1:c.")

    def test_nr_transcript_with_verified_pos_uses_n_prefix(self):
        h = self._hgvs("chrX", 500, "C", "T", "NR_024540.1", cds_pos=5, strand="+")
        assert h.startswith("NR_024540.1:n.")

    def test_xr_transcript_with_verified_pos_uses_n_prefix(self):
        h = self._hgvs("chr2", 200, "A", "G", "XR_001234.1", cds_pos=5, strand="+")
        assert h.startswith("XR_001234.1:n.")

    def test_insertion_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr1", 100, "A", "ATG", "NM_000059.4")
        assert "ins" in h
        assert ":g." in h

    def test_insertion_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr1", 100, "A", "ATG", "NM_000059.4", cds_pos=50, strand="+")
        assert "ins" in h
        assert h.startswith("NM_000059.4:c.")

    def test_deletion_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr1", 100, "ATG", "A", "NM_000059.4")
        assert "del" in h
        assert ":g." in h

    def test_deletion_with_verified_cds_range_uses_c_prefix(self):
        h = self._hgvs(
            "chr1", 100, "ATG", "A", "NM_000059.4", cds_pos=50, end_cds_pos=52, strand="+"
        )
        assert "del" in h
        assert h.startswith("NM_000059.4:c.")

    def test_delins_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr1", 100, "ATG", "CCC", "NM_000059.4")
        assert "delins" in h
        assert ":g." in h

    def test_enst_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr17", 43057051, "A", "T", "ENST00000357654.9")
        assert ":g." in h

    def test_enst_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr17", 43057051, "A", "T", "ENST00000357654.9", cds_pos=181, strand="+")
        assert h.startswith("ENST00000357654.9:c.")


# ══════════════════════════════════════════════════════════════════════════════
# D5 – Allele balance multiallelic
# ══════════════════════════════════════════════════════════════════════════════


class TestAlleleBalance:
    """Defect 5: AB must use the correct AD index per ALT allele."""

    def _extract(self, gt, keys, vals, alt_index=1):
        from pipeline.zygosity.extractor import ZygosityExtractor

        return ZygosityExtractor.extract(gt, keys, vals, alt_index=alt_index)

    def test_biallelic_ab_default(self):
        # REF=20, ALT=10 → AB = 10/30
        r = self._extract("0/1", ["GT", "AD", "DP"], ["0/1", "20,10", "30"])
        assert r.ab == pytest.approx(10 / 30, rel=1e-4)

    def test_multiallelic_alt1_ab(self):
        # REF=20, ALT1=10, ALT2=5 → for allele 1, AB = 10/35
        r = self._extract("0/1", ["GT", "AD", "DP"], ["0/1", "20,10,5", "35"], alt_index=1)
        assert r.ab == pytest.approx(10 / 35, rel=1e-4)

    def test_multiallelic_alt2_ab(self):
        # REF=20, ALT1=10, ALT2=5 → for allele 2, AB = 5/35
        r = self._extract("0/2", ["GT", "AD", "DP"], ["0/2", "20,10,5", "35"], alt_index=2)
        assert r.ab == pytest.approx(5 / 35, rel=1e-4)

    def test_multiallelic_alt1_and_alt2_differ(self):
        from pipeline.zygosity.extractor import ZygosityExtractor

        r1 = ZygosityExtractor.extract("0/1", ["GT", "AD"], ["0/1", "30,10,5"], alt_index=1)
        r2 = ZygosityExtractor.extract("0/2", ["GT", "AD"], ["0/2", "30,10,5"], alt_index=2)
        assert r1.ab != r2.ab

    def test_zero_total_depth_no_ab(self):
        r = self._extract("0/1", ["GT", "AD"], ["0/1", "0,0"], alt_index=1)
        assert r.ab is None

    def test_alt_index_clamped_to_valid(self):
        # alt_index=5 but only 2 ALT alleles → clamped, no crash
        r = self._extract("0/1", ["GT", "AD"], ["0/1", "20,10"], alt_index=5)
        assert r.ab is not None  # should not raise


# D6 (PGx subset allele exclusion) and ISSUE 6 (PGx hemizygous genotype
# handling) were removed 2026-09-10 along with the PGx package itself: a
# deliberate clinical-disclosure deletion, ruled by the human; see
# kim_pipeline/pipeline/reporting's commit history for the ruling.


# ══════════════════════════════════════════════════════════════════════════════
# D7 – BP7 synonymous_or_intronic
# ══════════════════════════════════════════════════════════════════════════════


class TestBP7:
    """Defect 7: BP7 must fire for synonymous/intronic variants without splice impact."""

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence

        return VariantEvidence(**kwargs)

    def _classify(self, ev):
        from pipeline.acmg.classifier import AcmgClassifier

        return AcmgClassifier().classify(ev)

    def test_bp7_fires_for_synonymous_non_splice(self):
        ev = self._ev(synonymous_or_intronic=True)
        r = self._classify(ev)
        assert "BP7" in r.criteria_met

    def test_bp7_not_fired_when_false(self):
        ev = self._ev(synonymous_or_intronic=False)
        r = self._classify(ev)
        assert "BP7" not in r.criteria_met

    def test_bp7_field_defaults_false(self):
        from pipeline.acmg.classifier import VariantEvidence

        ev = VariantEvidence()
        assert ev.synonymous_or_intronic is False


# ══════════════════════════════════════════════════════════════════════════════
# D8 – BP1 wired to ClinVar benign
# ══════════════════════════════════════════════════════════════════════════════


class TestBP1:
    """Defect 8: BP1 fires when a reputable source reports benign."""

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence

        return VariantEvidence(**kwargs)

    def _classify(self, ev):
        from pipeline.acmg.classifier import AcmgClassifier

        return AcmgClassifier().classify(ev)

    def test_bp1_fires_when_clinvar_benign_1star(self):
        ev = self._ev(bp1_reputable_source_benign=True)
        r = self._classify(ev)
        assert "BP1" in r.criteria_met

    def test_bp1_not_fired_when_none(self):
        ev = self._ev(bp1_reputable_source_benign=None)
        r = self._classify(ev)
        assert "BP1" not in r.criteria_met

    def test_bp1_not_fired_when_false(self):
        ev = self._ev(bp1_reputable_source_benign=False)
        r = self._classify(ev)
        assert "BP1" not in r.criteria_met


# ══════════════════════════════════════════════════════════════════════════════
# D9 – Duplicate @staticmethod removed
# ══════════════════════════════════════════════════════════════════════════════


class TestDuplicateStaticmethod:
    """Defect 9: EvidenceAggregator.clinvar_sig_to_score must be callable."""

    def test_clinvar_sig_to_score_callable(self):
        from pipeline.evidence.aggregator import EvidenceAggregator

        # If duplicate @staticmethod remains, Python would raise TypeError at import
        score = EvidenceAggregator.clinvar_sig_to_score("Pathogenic", 2)
        assert score == pytest.approx(1.0, rel=1e-4)

    def test_clinvar_sig_to_score_benign(self):
        from pipeline.evidence.aggregator import EvidenceAggregator

        score = EvidenceAggregator.clinvar_sig_to_score("Benign", 2)
        assert score == pytest.approx(0.0, rel=1e-4)

    def test_not_double_decorated(self):
        """Verify only one @staticmethod decorator exists on the method."""
        import ast
        import inspect
        import pipeline.evidence.aggregator as mod

        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "clinvar_sig_to_score":
                    decorator_names = [getattr(d, "id", None) for d in node.decorator_list]
                    assert decorator_names.count("staticmethod") == 1, (
                        f"Expected 1 @staticmethod, got {decorator_names.count('staticmethod')}"
                    )


# ══════════════════════════════════════════════════════════════════════════════
# D10 – Version synchronisation
# ══════════════════════════════════════════════════════════════════════════════


class TestVersionSync:
    """Defect 10, REPLACED 2026-09-11 (human-approved: "make 8 Kim's single
    version source"). The old class asserted five hand-typed "12.0.0"
    literals agreed with EACH OTHER -- pyproject.toml and pipeline/__init__.py
    -- while the API said 8.0.0 and every report and --version said v8, and
    nothing in the code read the 12.0.0 values at all. Agreement between
    copies is not a source.

    Now there is one: pipeline/reporting/component_identity.py::VERSION
    ("8.0.0"), with the label PIPELINE_VERSION ("... v8") derived from it.
    Each test below reads a real surface and compares it to that module;
    the last one scans the consumer files so a hand-typed number anywhere
    fails even if every surface test still happens to agree.
    """

    def _identity(self):
        from pipeline.reporting import component_identity

        return component_identity

    def test_single_source_and_its_two_renderings(self):
        ci = self._identity()
        assert ci.VERSION == "8.0.0"
        assert ci.PIPELINE_VERSION == f"{ci.COMPONENT_NAME} v{ci.VERSION.split('.')[0]}"

    def test_api_openapi_and_health_read_the_source(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from api.main import app

        ci = self._identity()
        assert app.openapi()["info"]["version"] == ci.VERSION
        assert TestClient(app).get("/health").json()["version"] == ci.VERSION

    def test_report_json_pipeline_reads_the_source(self, tmp_path):
        import json
        from pathlib import Path

        from tests.test_reporting_stage import TestIssue3ClinicalReportOverhaul

        result = TestIssue3ClinicalReportOverhaul()._run(tmp_path)
        payload = json.loads(Path(result.json_path).read_text())
        assert payload["pipeline"] == self._identity().PIPELINE_VERSION

    def test_cli_version_reads_the_source(self):
        import os
        import subprocess
        import sys
        from pathlib import Path

        kim_root = Path(__file__).resolve().parents[1]
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        out = subprocess.run(
            [sys.executable, str(kim_root / "main.py"), "--version"],
            cwd=str(kim_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.strip()
        assert out == self._identity().PIPELINE_VERSION

    def test_package_metadata_reads_the_source(self):
        # setuptools' own expansion of pyproject.toml -- the value a build
        # would stamp into the wheel -- and proof the version is dynamic
        # (read from the module), not a literal.
        import os
        import tomllib
        from pathlib import Path

        from setuptools.config.pyprojecttoml import read_configuration

        kim_root = Path(__file__).resolve().parents[1]
        raw = tomllib.loads((kim_root / "pyproject.toml").read_text(encoding="utf-8"))
        assert "version" not in raw["project"]
        assert "version" in raw["project"]["dynamic"]
        assert raw["tool"]["setuptools"]["dynamic"]["version"] == {
            "attr": "pipeline.reporting.component_identity.VERSION"
        }
        cwd = os.getcwd()
        try:
            os.chdir(kim_root)
            expanded = read_configuration(kim_root / "pyproject.toml", expand=True)
        finally:
            os.chdir(cwd)
        assert expanded["project"]["version"] == self._identity().VERSION
        # The description is static text; it must carry the same label.
        assert raw["project"]["description"].startswith(self._identity().PIPELINE_VERSION + " ")

    def test_package_init_constants_read_the_source(self):
        import pipeline

        ci = self._identity()
        assert pipeline.__version__ == ci.VERSION
        assert pipeline.PIPELINE_VERSION == ci.PIPELINE_VERSION
        # Removed, not re-pointed: nothing ever read them, and no checkpoint
        # or report stores a format version (resume compares only
        # checkpoint["reference_versions"]). A name that promises a format
        # check that does not exist is worse than no name.
        assert not hasattr(pipeline, "CHECKPOINT_VERSION")
        assert not hasattr(pipeline, "REPORT_VERSION")

    def test_no_hand_typed_version_anywhere_in_the_consumers(self):
        import re
        from pathlib import Path

        kim_root = Path(__file__).resolve().parents[1]
        label = self._identity().PIPELINE_VERSION
        consumers = (
            "api/main.py",
            "pyproject.toml",
            "pipeline/__init__.py",
            "main.py",
            "pipeline/reporting/stage.py",
            "pipeline/reporting/pdf_report.py",
            "pipeline/orchestration/runner.py",
            "serve_api.py",
        )
        assignment = re.compile(r"""(?i)\b(__version__|[a-z_]*version)\s*[=:]\s*["']\s*v?\d""")
        vnumber = re.compile(r"""(?<![/\w.])v\d+(?:\.\d+)*\b(?!/)""")
        allowed = {'python_version = "3.10"'}  # mypy's target interpreter, not Kim's version
        hits = []
        for rel in consumers:
            for lineno, line in enumerate(
                (kim_root / rel).read_text(encoding="utf-8").splitlines(), 1
            ):
                if line.strip() in allowed:
                    continue
                for pat in (assignment, vnumber):
                    m = pat.search(line)
                    # A line may carry the version only as the rendered label
                    # itself; any other number is hand-typed.
                    if m and label not in line:
                        hits.append(f"{rel}:{lineno}: {line.strip()[:100]}")
                        break
        assert hits == []


# ══════════════════════════════════════════════════════════════════════════════
# D11 – PS1 / PM5 mutual exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestPS1PM5MutualExclusion:
    """Defect 11: PS1 and PM5 must never both fire for the same variant."""

    def _clf(self):
        from pipeline.acmg.classifier import AcmgClassifier

        return AcmgClassifier()

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence

        return VariantEvidence(**kwargs)

    def test_ps1_fires_when_same_aa_pathogenic(self):
        r = self._clf().classify(self._ev(same_aa_pathogenic=True, is_missense=True))
        assert "PS1" in r.criteria_met

    def test_pm5_suppressed_when_ps1_active(self):
        """If PS1 is active, PM5 must not fire — no double-counting."""
        ev = self._ev(
            same_aa_pathogenic=True,
            novel_aa_at_known_pathogenic_codon=True,
            is_missense=True,
        )
        r = self._clf().classify(ev)
        assert "PS1" in r.criteria_met
        assert "PM5" not in r.criteria_met, (
            "PM5 must not fire when PS1 is active (ACMG PS1/PM5 mutual exclusion)"
        )

    def test_pm5_fires_independently_without_ps1(self):
        ev = self._ev(
            same_aa_pathogenic=None,
            novel_aa_at_known_pathogenic_codon=True,
            is_missense=True,
        )
        r = self._clf().classify(ev)
        assert "PM5" in r.criteria_met
        assert "PS1" not in r.criteria_met

    def test_neither_fires_when_no_evidence(self):
        ev = self._ev(is_missense=True)
        r = self._clf().classify(ev)
        assert "PS1" not in r.criteria_met
        assert "PM5" not in r.criteria_met

    def test_pm5_not_fired_for_non_missense(self):
        ev = self._ev(novel_aa_at_known_pathogenic_codon=True, is_missense=False)
        r = self._clf().classify(ev)
        assert "PM5" not in r.criteria_met


# ══════════════════════════════════════════════════════════════════════════════
# D12 – BLAST results propagated into annotation
# ══════════════════════════════════════════════════════════════════════════════


class TestBlastPropagation:
    """Defect 12: blast_result_data must be merged into annotation variants."""

    def _make_blast_data(self, chrom, pos, ref, alt):
        seq_id = f"{chrom}:{pos}:{ref}:{alt}"
        return {
            "hit_count": 1,
            "database": "nr",
            "hits": [
                {
                    "sequence_id": seq_id,
                    "identity": 98.5,
                    "evalue": 1e-10,
                    "description": "Homo sapiens BRCA1",
                }
            ],
        }

    def test_blast_hits_merged_into_variant(self):
        """Simulate what runner does: merge BLAST hits keyed by chrom:pos:ref:alt."""
        from pipeline.orchestration.runner import BLAST_SUMMARY_NOTE

        blast_data = self._make_blast_data("chr17", 43057051, "A", "T")

        # Simulate annotation result.annotation dict
        annotation = {
            "variants": [
                {"chrom": "chr17", "pos": 43057051, "ref": "A", "alt": "T", "gene": "BRCA1"}
            ]
        }

        # Reproduce the runner merge logic
        blast_hits_by_id: Dict[str, list] = {}
        for hit in blast_data.get("hits", []):
            seq_id = hit.get("sequence_id", "")
            blast_hits_by_id.setdefault(seq_id, []).append(hit)

        for var in annotation["variants"]:
            bid = f"{var.get('chrom', '')}:{var.get('pos', '')}:{var.get('ref', '')}:{var.get('alt', '')}"
            hits = blast_hits_by_id.get(bid, [])
            if hits:
                var["blast_hits"] = hits

        annotation["blast_summary"] = {
            "total_hits": blast_data.get("hit_count", 0),
            "blast_db": blast_data.get("database", ""),
            "note": BLAST_SUMMARY_NOTE,
        }

        assert "blast_hits" in annotation["variants"][0]
        assert annotation["variants"][0]["blast_hits"][0]["identity"] == 98.5
        assert "blast_summary" in annotation
        assert "supporting information" in annotation["blast_summary"]["note"]

    def test_blast_note_does_not_override_acmg(self):
        """The BLAST summary note must state it does not override ACMG evidence.

        Before fix: this built `blast_data` and then discarded it, asserting
        a hardcoded `note` string literal against itself two lines below --
        a tautology that could never fail regardless of blast_data's content
        or runner.py's actual behaviour. Reproducing the sibling test's merge
        logic was NOT enough on its own -- the sibling had the identical
        hole, since both were independently transcribing the same literal
        rather than reading it from anywhere. `grep -rn "do not override
        ACMG evidence"` used to return three hits (runner.py:757 plus one
        in each test) and deleting runner.py:757 left both tests green.

        FIX 12 extracted the literal to a module-level constant,
        `BLAST_SUMMARY_NOTE` in pipeline/orchestration/runner.py, which
        this test now imports instead of retyping -- so editing or
        removing the disclaimer in source is what this test is actually
        pinned to.
        """
        from pipeline.orchestration.runner import BLAST_SUMMARY_NOTE

        blast_data = self._make_blast_data("chr1", 100, "A", "T")

        annotation: Dict = {"variants": [{"chrom": "chr1", "pos": 100, "ref": "A", "alt": "T"}]}
        annotation["blast_summary"] = {
            "total_hits": blast_data.get("hit_count", 0),
            "blast_db": blast_data.get("database", ""),
            "note": BLAST_SUMMARY_NOTE,
        }

        assert "not override ACMG evidence" in annotation["blast_summary"]["note"]

    # FIX #9 (2026-08-31): test_no_blast_data_does_not_crash used to sit
    # here. It built a local `annotation` dict, set `blast_result_data =
    # None`, guarded them with a literal `if blast_result_data and
    # isinstance(...): pass` that could never execute (`blast_result_data`
    # was always `None`) and did nothing even if it had (`pass`), then
    # asserted `"variants" in annotation` -- true only because the test
    # had just put that key there itself two lines above. It never
    # imported or called runner.py, so it could not have caught a real
    # regression in the guard it claimed to describe. Removed and
    # replaced by tests/test_vcf_only_mode.py::
    # TestNoBlastDataLeavesAnnotationUnmerged::
    # test_no_blast_data_does_not_add_blast_summary, which drives the
    # real guard at runner.py:752 through an actual `PipelineRunner.run()`
    # call and is confirmed live by mutation (see that test's docstring).


# ══════════════════════════════════════════════════════════════════════════════
# D13 – VEP HGVS propagation
# ══════════════════════════════════════════════════════════════════════════════


class TestVepHgvsPropagation:
    """Defect 13: VEP HGVSc and HGVSp must populate AnnotatedVariant."""

    def test_annotated_variant_has_vep_hgvs_fields(self):
        from pipeline.annotation.stage import AnnotatedVariant

        v = AnnotatedVariant(chrom="chr17", pos=43057051, ref="A", alt="T")
        assert hasattr(v, "vep_hgvs_c")
        assert hasattr(v, "vep_hgvs_p")

    def test_vep_hgvs_defaults_empty(self):
        from pipeline.annotation.stage import AnnotatedVariant

        v = AnnotatedVariant(chrom="chr1", pos=100, ref="A", alt="T")
        assert v.vep_hgvs_c == ""
        assert v.vep_hgvs_p == ""

    def test_vep_hgvs_populated(self):
        from pipeline.annotation.stage import AnnotatedVariant

        v = AnnotatedVariant(
            chrom="chr17",
            pos=43057051,
            ref="A",
            alt="T",
            vep_hgvs_c="NM_007294.4:c.5266dup",
            vep_hgvs_p="NP_009225.1:p.Gln1756fs",
        )
        assert v.vep_hgvs_c == "NM_007294.4:c.5266dup"
        assert v.vep_hgvs_p == "NP_009225.1:p.Gln1756fs"

    def test_extract_csq_hgvs_function_exists(self):
        from pipeline.annotation.stage import _extract_csq_hgvs

        assert callable(_extract_csq_hgvs)

    def test_extract_csq_hgvs_empty_info(self):
        from pipeline.annotation.stage import _extract_csq_hgvs

        hc, hp = _extract_csq_hgvs("DP=30;AF=0.5", [])
        assert hc == ""
        assert hp == ""

    def test_extract_csq_hgvs_parses_csq(self):
        from pipeline.annotation.stage import _extract_csq_hgvs

        # Minimal CSQ with HGVSc and HGVSp fields
        csq_fields = ["Allele", "Consequence", "SYMBOL", "HGVSc", "HGVSp"]
        csq_value = "T|missense_variant|BRCA1|NM_007294.4:c.5266A>T|NP_009225.1:p.Lys1756Asn"
        info = f"DP=30;CSQ={csq_value}"
        hc, hp = _extract_csq_hgvs(info, csq_fields)
        assert hc == "NM_007294.4:c.5266A>T"
        assert hp == "NP_009225.1:p.Lys1756Asn"

    def test_nm_g_notation_filtered_out(self):
        """VEP should never emit NM_:g. but if it does, we filter it."""
        from pipeline.annotation.stage import _extract_csq_hgvs

        csq_fields = ["Allele", "Consequence", "SYMBOL", "HGVSc", "HGVSp"]
        # Pathological case: invalid NM_:g. notation
        csq_value = "T|missense_variant|BRCA1|NM_007294.4:g.43057051A>T|"
        info = f"DP=30;CSQ={csq_value}"
        hc, hp = _extract_csq_hgvs(info, csq_fields)
        assert hc == "", f"NM_:g. notation must be filtered out, got: {hc!r}"


# ══════════════════════════════════════════════════════════════════════════════
# D14 – ClinVar cache
# ══════════════════════════════════════════════════════════════════════════════


class TestClinVarCache:
    """Defect 14: ClinVar lookup() must not raise NameError; cache must work."""

    def _lkp(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        return ClinVarLookup(cfg={"clinvar": {"backend": "local", "tsv_path": None}})

    def test_lookup_does_not_raise_nameerror(self):
        """Before fix, lookup() raised NameError: name 'cache_key' is not defined.

        Mocked because the test's config keys don't match what __init__
        reads, so backend silently defaults to 'api' instead of 'local'.
        Mock prevents the network call; it does not route to local TSV as
        this docstring used to claim.

        Same root cause as the ClinVar config key-name defect (commit
        b2e819c, config_validator._validate_clinvar_keys / test coverage
        in test_clinvar_config_e.py): `_lkp()` passes `tsv_path`, but
        ClinVarLookup.__init__ only reads `tsv_gz_path`. That defect fix
        (human-ruled: warn only, not accept-both -- see
        clinvar-lookup-silently-ignores-tsv-path-config-and-falls-back-to-live-network
        on the board) makes a mis-keyed config warn at validate_config
        time, but does NOT change ClinVarLookup's own routing -- a direct
        ClinVarLookup(cfg=...) construction (as this test does, bypassing
        validate_config) still silently falls to 'api' on this same
        mis-key. So this mock still stands even with that defect fixed.
        The constructor was deliberately not changed to accept both keys;
        see b2e819c.

        DO NOT REMOVE THIS MOCK. It was previously described here as
        something to remove "when the key mismatch in `_lkp()` is
        corrected" -- that claim was tested directly (2026-09-10) and is
        false: removing the mock does not reach the local TSV path.
        `_lkp()`'s mis-keyed config makes `self._backend` stay `"api"`
        regardless (see the routing above), so `lookup()` calls
        `_api_lookup()` and reaches a live network request to NCBI,
        which returns real data on an environment with outbound network
        -- confirmed against a real BRCA1 coordinate. This test exercises
        the API-FALLBACK path, not the local one, and the mock is the
        only thing between this suite and a live NCBI call on that path.
        Reaching local mode instead would require fixing the mis-keyed
        config `_lkp()` passes -- the config keys themselves are a
        human-ruled design decision (tsv_path is a correct key for a
        different section; accepting it under `clinvar:` would hide
        exactly this class of copy-paste error) and are not changed
        here or by this test.
        """
        lkp = self._lkp()
        with patch.object(type(lkp), "_api_lookup", return_value=None):
            result = lkp.lookup("chr17", 43057051, "A", "T")
        assert result is None  # no local TSV loaded, but should not raise

    def test_lookup_caches_result(self):
        lkp = self._lkp()
        lkp.lookup("chr1", 100, "A", "T")
        lkp.lookup("chr1", 100, "A", "T")
        stats = lkp.cache_stats()
        assert stats["hits"] >= 1

    def test_cache_hit_increments_counter(self):
        lkp = self._lkp()
        lkp.lookup("chr2", 200, "G", "C")
        hits_before = lkp.cache_stats()["hits"]
        lkp.lookup("chr2", 200, "G", "C")
        assert lkp.cache_stats()["hits"] == hits_before + 1

    def test_different_variants_separate_keys(self):
        lkp = self._lkp()
        lkp.lookup("chr1", 100, "A", "T")
        lkp.lookup("chr1", 101, "G", "C")
        assert lkp.cache_stats()["misses"] >= 2

    def test_chr_prefix_normalisation(self):
        """chr1 and 1 must map to the same cache entry."""
        lkp = self._lkp()
        lkp.lookup("1", 100, "A", "T")
        hits_before = lkp.cache_stats()["hits"]
        lkp.lookup("chr1", 100, "A", "T")
        assert lkp.cache_stats()["hits"] == hits_before + 1

    def test_none_result_cached(self):
        lkp = self._lkp()
        lkp.lookup("chrX", 999999, "A", "T")
        hits_before = lkp.cache_stats()["hits"]
        lkp.lookup("chrX", 999999, "A", "T")
        assert lkp.cache_stats()["hits"] == hits_before + 1

    def test_check_same_codon_no_nameerror(self):
        """check_same_codon_pathogenic must not raise due to undefined variables."""
        lkp = self._lkp()
        result = lkp.check_same_codon_pathogenic("chr17", 43057051, "A", "T", "K", "N")
        assert result == (None, None)  # no local data, but should not raise

    def test_sig_to_score_unknown_returns_none(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        assert ClinVarLookup.sig_to_score("Mixed significance", 0) is None

    def test_sig_to_score_unknown_significance_returns_none(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        assert ClinVarLookup.sig_to_score("Unknown significance", 0) is None

    def test_sig_to_score_pathogenic_returns_1(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        assert ClinVarLookup.sig_to_score("Pathogenic", 3) == pytest.approx(1.0)

    def test_sig_to_score_benign_returns_0(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        assert ClinVarLookup.sig_to_score("Benign", 2) == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# D15 – PP2 missense constraint
# ══════════════════════════════════════════════════════════════════════════════


class TestPP2MissenseConstraint:
    """Defect 15: PP2 must use missense constraint, not LoF intolerance."""

    def _clf(self):
        from pipeline.acmg.classifier import AcmgClassifier

        return AcmgClassifier()

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence

        return VariantEvidence(**kwargs)

    def test_pp2_fires_when_missense_constrained(self):
        ev = self._ev(is_missense=True, missense_constrained=True)
        r = self._clf().classify(ev)
        assert "PP2" in r.criteria_met

    def test_pp2_not_fired_when_not_missense_constrained(self):
        ev = self._ev(is_missense=True, missense_constrained=False, lof_gene_intolerant=True)
        r = self._clf().classify(ev)
        # lof_gene_intolerant alone must NOT trigger PP2
        assert "PP2" not in r.criteria_met, "PP2 must not fire based on LoF intolerance alone"

    def test_pp2_not_fired_for_non_missense(self):
        ev = self._ev(is_missense=False, missense_constrained=True)
        r = self._clf().classify(ev)
        assert "PP2" not in r.criteria_met

    def test_variant_evidence_has_missense_constrained_field(self):
        from pipeline.acmg.classifier import VariantEvidence

        ev = VariantEvidence()
        assert hasattr(ev, "missense_constrained")
        assert ev.missense_constrained is False

    def test_constraint_record_has_oe_mis(self):
        from pipeline.constraint.lookup import ConstraintRecord

        rec = ConstraintRecord(gene="BRCA1", pli=0.99, loeuf=0.1, oe_mis=0.5, mis_z=3.5)
        assert rec.oe_mis == 0.5
        assert rec.mis_z == 3.5

    def test_is_missense_constrained_oe_mis_threshold(self):
        from pipeline.constraint.lookup import ConstraintRecord

        # oe_mis < 0.8 → constrained
        rec_constrained = ConstraintRecord(gene="BRCA1", pli=0.5, loeuf=0.5, oe_mis=0.6)
        assert rec_constrained.is_missense_constrained() is True

        # oe_mis >= 0.8 → not constrained
        rec_unconstrained = ConstraintRecord(gene="TTN", pli=0.1, loeuf=0.9, oe_mis=0.95)
        assert rec_unconstrained.is_missense_constrained() is False

    def test_is_missense_constrained_mis_z_threshold(self):
        from pipeline.constraint.lookup import ConstraintRecord

        # mis_z > 3.09 → constrained
        rec = ConstraintRecord(gene="BRCA2", pli=0.5, loeuf=0.5, oe_mis=0.9, mis_z=3.5)
        assert rec.is_missense_constrained() is True

        # mis_z <= 3.09 → not constrained (oe_mis also above threshold)
        rec2 = ConstraintRecord(gene="MUC5B", pli=0.1, loeuf=0.9, oe_mis=0.9, mis_z=1.0)
        assert rec2.is_missense_constrained() is False

    def test_constraint_lookup_has_is_missense_constrained(self):
        from pipeline.constraint.lookup import GnomadConstraintLookup

        lkp = GnomadConstraintLookup(cfg={})
        # No data loaded → should return False without crashing
        result = lkp.is_missense_constrained("BRCA1")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# D4 – GFF3 phase handling
# ══════════════════════════════════════════════════════════════════════════════


class TestGff3PhaseHandling:
    """Defect 4: GFF3 CDS phase must be applied to codon frame calculation."""

    def test_cds_record_has_phase_field(self):
        from pipeline.annotation.codon_provider import CdsRecord

        # Signature: (chrom, start, end, strand, phase, transcript_id)
        rec = CdsRecord("chr17", 43044295, 43044522, "+", 0, "NM_007294.4")
        assert rec.phase == 0

    def test_phase_0_no_offset(self):
        """phase=0 means first base of exon is first base of a codon."""
        from pipeline.annotation.codon_provider import CdsRecord

        rec = CdsRecord("chr1", 100, 199, "+", 0, "NM_000001.1")
        assert rec.phase == 0

    def test_phase_1_shifts_frame(self):
        """phase=1 means 1 base of the first codon is in the previous exon."""
        from pipeline.annotation.codon_provider import CdsRecord

        rec = CdsRecord("chr1", 100, 199, "+", 1, "NM_000001.1")
        assert rec.phase == 1

    def test_phase_2_shifts_frame(self):
        from pipeline.annotation.codon_provider import CdsRecord

        rec = CdsRecord("chr1", 100, 199, "+", 2, "NM_000001.1")
        assert rec.phase == 2

    def test_phase_applied_to_cds_position(self, tmp_path, monkeypatch):
        """Verify GFF3 phase shifts the codon frame used to read/translate a variant.

        PROBE-CONFIRMED REWRITE (T2-F2, audit finding): the previous version of
        this test reimplemented codon_provider.py's own phase arithmetic locally
        (`cds_pos = cds_offset + pos_in_exon - phase_offset; codon_index = cds_pos
        % 3` — byte-identical to `_classify_snv_full`'s `cds_pos = cds_offset +
        pos_in_exon - _phase_offset` / `codon_index = cds_pos % 3`,
        codon_provider.py:450/453) instead of calling the real
        FastaCodonContextProvider. Probe: flipping the sign in the real function
        (`+ _phase_offset` instead of `-`) left the old test green — it pinned its
        own copy of the formula, not the function it claimed to guard.

        This version drives the real provider end-to-end against a real (temp-file)
        GFF3 + FASTA — no formula is duplicated here.

        ORACLE CORRECTED (phase!=0 codon fix). The expected values were
        previously ("missense", "GCA", "ACA", "A", "T"), described here as "the
        exact output of the real (correct) function ... confirmed by direct
        execution". They were the exact output of the real function, but the
        function was WRONG: `_fetch_codon` consumed a phase-adjusted
        `codon_cds_start` as though it were a raw exon offset, so every codon of
        a phase!=0 transcript was read `phase` bases upstream. Executing the code
        confirms what the code DOES, never what it SHOULD do, so taking the
        oracle from the implementation locked the defect in as the guard.

        The rewrite that produced this test was still right, and its probe (flip
        the sign of `_phase_offset`, watch the old test stay green) was a real
        known-positive — it proved the test could DETECT a change. What it could
        not prove, and what no self-derived oracle can, is that the value being
        pinned is the CORRECT one.

        The value below is derived by hand instead, from the GFF3 spec and the
        genetic code, and only then compared against the fixed implementation:
          exon starts 101, phase=1 -> "remove 1 base to reach the first base of
          the next codon" -> first complete codon starts at 102.
          pos 105 is offset 3 from 102 -> codon_index 0 -> codon spans 105-107.
          Sequence at 105,106,107 = C,A,T -> ref codon CAT; C>A at index 0 gives
          AAT. CAT = His (H), AAT = Asn (N) -> a missense.
        """
        monkeypatch.setattr(
            "pipeline.annotation.codon_provider._FastaReader._check_samtools",
            staticmethod(lambda: False),
        )
        from pipeline.annotation.codon_provider import CdsRecord, FastaCodonContextProvider

        # Single-exon CDS, chr1, + strand, phase=1.
        gff_path = tmp_path / "phase.gff3"
        gff_path.write_text(
            "chr1\tsrc\tCDS\t101\t130\t.\t+\t1\tParent=NM_TEST.1\n", encoding="utf-8"
        )
        seq = ("N" * 100) + "GATGCATGGATCCATGAATTCCGGATCCTAGGAA" + ("N" * 20)
        fasta_path = tmp_path / "phase.fasta"
        fasta_path.write_text(f">chr1\n{seq}\n", encoding="utf-8")

        provider = FastaCodonContextProvider(gff_path=str(gff_path), fasta_path=str(fasta_path))
        assert provider._available, "fixture GFF3/FASTA must load for this probe to mean anything"
        provider._cds_map = {"NM_TEST.1": [CdsRecord("chr1", 101, 130, "+", 1, "NM_TEST.1")]}

        result = provider.get_codon_and_aa("chr1", 105, "C", "A", "NM_TEST.1")
        assert result == ("missense", "CAT", "AAT", "H", "N"), (
            f"real FastaCodonContextProvider.get_codon_and_aa output for this "
            f"phase=1 fixture is not the hand-derived correct value; got {result!r}. "
            f"Expected codon CAT at 105-107 (phase=1 -> first complete codon at 102). "
            f"Getting ('missense', 'GCA', 'ACA', 'A', 'T') back means the phase "
            f"offset is no longer reaching _fetch_codon and codons are being read "
            f"one base upstream again."
        )


# ══════════════════════════════════════════════════════════════════════════════
# D1 – 5-tuple unpack from get_codon_and_aa()
# ══════════════════════════════════════════════════════════════════════════════


class TestCodonProviderTuple:
    """Defect 1: get_codon_and_aa() returns 5 values — all must be unpacked."""

    def test_codon_provider_returns_5_tuple(self):
        """get_codon_and_aa() returns a 5-tuple on success; annotation stage unpacks all 5."""
        from pipeline.annotation.codon_provider import FastaCodonContextProvider

        provider = FastaCodonContextProvider(gff_path="", fasta_path="")

        # Mock the internal _classify_snv_full to return the 5-tuple
        with patch.object(
            provider,
            "_classify_snv_full",
            return_value=("missense_variant", "AAA", "AAT", "K", "N"),
        ):
            # Also mock _available to True and _cds_map to contain the transcript
            provider._available = True
            provider._cds_map = {
                "NM_000001.1": [
                    MagicMock(
                        chrom="chr1",
                        start=100,
                        end=200,
                        strand="+",
                        phase=0,
                        transcript_id="NM_000001.1",
                    )
                ]
            }
            result = provider.get_codon_and_aa("chr1", 100, "A", "T", "NM_000001.1")

        assert len(result) == 5, f"Expected 5-tuple, got {len(result)}-tuple: {result}"
        consequence, ref_codon, alt_codon, ref_aa, alt_aa = result
        assert consequence == "missense_variant"
        assert ref_aa == "K"
        assert alt_aa == "N"

    def _run_annotation_stage_with_codon_provider(self, tmp_path, monkeypatch, get_codon_and_aa):
        """Drives AnnotationStage.run() for real, with the GFF3/RNA-analyser
        dependencies faked out (no real GFF3/FASTA needed to reach the target
        code) but the real "AI engine sequence inputs from codon provider"
        unpack block in stage.py (inside AnnotationStage.run()'s per-variant
        loop: `consequence, ref_codon, alt_codon, ref_aa, alt_aa =
        self._codon_provider.get_codon_and_aa(...)`) running unmodified.
        Returns the single resulting AnnotatedVariant.
        """
        from pipeline.annotation.stage import AnnotationStage

        vcf_path = tmp_path / "variant.vcf"
        vcf_path.write_text(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr1\t105\t.\tC\tA\t.\tPASS\t.\n",
            encoding="utf-8",
        )

        class _FakeGffIndex:
            def lookup(self, chrom, pos):
                return "GENE1", "NM_TEST.1"

        class _FakeRnaAnalyser:
            def get_transcript(self, transcript_id):
                return MagicMock(strand="+")  # truthy, and stage.py reads .strand off it

            def genomic_to_cds_pos(self, chrom, pos, transcript_id):
                return None  # falls back to genomic (g.) HGVS notation -- fine, not under test

            def classify_region(self, chrom, pos, transcript_id):
                return "exonic"

        class _FakeCodonProvider:
            _available = True
            _fasta = MagicMock(fetch=MagicMock(return_value=""))
            _cds_map: Dict = {}

            def get_codon_change(self, chrom, pos, ref, alt, transcript_id):
                return "missense"  # drives var.consequence to "missense_variant"

            def get_codon_and_aa(self, chrom, pos, ref, alt, transcript_id):
                return get_codon_and_aa(chrom, pos, ref, alt, transcript_id)

        stage = AnnotationStage(cfg={})
        monkeypatch.setattr(stage, "_get_gff_index", lambda: _FakeGffIndex())
        monkeypatch.setattr(stage, "_get_rna_analyser", lambda: _FakeRnaAnalyser())
        stage._codon_provider = _FakeCodonProvider()

        result = stage.run(str(vcf_path), str(tmp_path), sample_id="TESTSAMPLE")
        assert len(result.variants) == 1
        return result.variants[0]

    def test_annotation_stage_unpack_accepts_5tuple(self, tmp_path, monkeypatch):
        """T2-F3 REWRITE: drives the REAL annotation-stage unpack (stage.py's
        "AI engine sequence inputs from codon provider" block, inside
        AnnotationStage.run()) against a fake codon provider returning a real
        5-tuple. Before this rewrite, the docstring claimed "the annotation-
        stage unpack code handles 5 values" but the body only unpacked a
        hand-typed tuple literal — it never touched the annotation stage at
        all. This version proves the real unpack reaches AnnotatedVariant.
        """
        var = self._run_annotation_stage_with_codon_provider(
            tmp_path,
            monkeypatch,
            get_codon_and_aa=lambda *a: ("missense", "GCA", "ACA", "Ala", "Thr"),
        )
        assert var.consequence == "missense_variant"
        assert var.wildtype_aa == "Ala", "real 5-tuple unpack must reach var.wildtype_aa"
        assert var.mutant_aa == "Thr", "real 5-tuple unpack must reach var.mutant_aa"

    def test_5tuple_unpack_does_not_raise(self, tmp_path, monkeypatch):
        """T2-F3 REWRITE, and a correction of this test's own former premise:
        its old docstring said "If still 4-tuple, this would raise ValueError" —
        but the real unpack site (stage.py, inside AnnotationStage.run()) wraps
        the unpack in `except Exception: pass` ("best-effort only"), so a
        too-few-values ValueError is SWALLOWED, never raised out of run().
        Simulates the pre-fix defect (get_codon_and_aa returning only 3 values)
        against the real code and verifies the actual real behavior: run()
        completes without raising, and the AA fields it would have set are
        left at their default (None) rather than fabricated.
        """
        var = self._run_annotation_stage_with_codon_provider(
            tmp_path,
            monkeypatch,
            get_codon_and_aa=lambda *a: ("missense", "GCA", "ACA"),  # too few to unpack into 5
        )
        assert var.consequence == "missense_variant"
        assert var.wildtype_aa is None, "swallowed unpack failure must not fabricate an AA"
        assert var.mutant_aa is None, "swallowed unpack failure must not fabricate an AA"


# ══════════════════════════════════════════════════════════════════════════════
# D2 – bcftools norm integration
# ══════════════════════════════════════════════════════════════════════════════


class TestBcftoolsNormIntegration:
    """Defect 2: bcftools norm must be invoked with --fasta-ref.

    Before this rewrite: both tests below hand-built a `norm_cmd` list
    literal and asserted it contained what had just been typed into it --
    a tautology that never called `VariantCallingStage.run()` at all, so
    it stayed green even with the real command's `--fasta-ref` deleted
    entirely (confirmed by probe, see this class's own docstring history
    on the card). The real command is built INLINE inside `run()`
    (pipeline/variant_calling/stage.py:121-156), behind two real
    subprocess calls (a `bcftools --version` probe, then the norm call
    itself) -- there was no separately-callable function to import.

    Fix: drive `run()` for real, with every external dependency it
    touches intercepted -- `freebayes_runner.is_available`/`run_freebayes`
    mocked so no real FreeBayes is needed, `subprocess.run` replaced with
    a fake that recognises the `--version` probe and the real `norm`
    call (capturing its exact argv) and returns success for both, and
    `apply_pass_filter` mocked so the test never needs the intercepted
    norm call to have actually produced a parseable VCF. The captured
    argv IS the command production actually built -- not a copy of it.
    """

    def _run_stage_and_capture_norm_cmd(self, tmp_path, reference_fasta="/path/to/hg38.fa"):
        """Drives VariantCallingStage.run() for real and returns the exact
        argv list passed to the real `bcftools norm` subprocess call."""
        from pipeline.variant_calling.filtering import FilterSummary
        from pipeline.variant_calling.stage import VariantCallingStage

        captured: Dict = {}

        def _fake_spawn_tracked(cmd, *args, **kwargs):
            if cmd[:2] == ["bcftools", "--version"]:
                proc = MagicMock()
                proc.returncode = 0
                proc.communicate = MagicMock(return_value=("", ""))
                return proc
            if cmd[:2] == ["bcftools", "norm"]:
                captured["cmd"] = cmd
                proc = MagicMock()
                proc.returncode = 0
                proc.communicate = MagicMock(return_value=("", ""))
                return proc
            raise AssertionError(f"unexpected spawn_tracked call in this probe: {cmd!r}")

        with (
            patch(
                "pipeline.variant_calling.stage.freebayes_runner.is_available", return_value=True
            ),
            patch("pipeline.variant_calling.stage.freebayes_runner.run_freebayes"),
            patch("pipeline.variant_calling.stage.spawn_tracked", side_effect=_fake_spawn_tracked),
            patch("pipeline.variant_calling.stage.apply_pass_filter", return_value=FilterSummary()),
        ):
            stage = VariantCallingStage({"variant_calling": {"threads": 1}})
            stage.run(
                bam_path="fake.bam",
                reference_fasta=reference_fasta,
                output_dir=str(tmp_path),
                sample_id="TESTSAMPLE",
            )

        assert "cmd" in captured, (
            "bcftools norm was never invoked -- the --version probe must have failed"
        )
        return captured["cmd"]

    def test_norm_command_includes_fasta_ref(self, tmp_path):
        """Verify the REAL norm command (captured from VariantCallingStage.run(), not
        a hand-typed copy) contains --fasta-ref pointing at the real reference passed in."""
        reference_fasta = "/path/to/hg38.fa"
        norm_cmd = self._run_stage_and_capture_norm_cmd(tmp_path, reference_fasta)
        assert "--fasta-ref" in norm_cmd
        idx = norm_cmd.index("--fasta-ref")
        assert norm_cmd[idx + 1] == reference_fasta

    def test_norm_command_splits_multiallelics(self, tmp_path):
        norm_cmd = self._run_stage_and_capture_norm_cmd(tmp_path)
        assert "--multiallelics" in norm_cmd
        idx = norm_cmd.index("--multiallelics")
        assert norm_cmd[idx + 1] == "-"  # "-" means split

    @pytest.mark.skipif(
        __import__("shutil").which("bcftools") is None,
        reason="bcftools not installed",
    )
    def test_bcftools_available(self):
        import subprocess

        result = subprocess.run(["bcftools", "--version"], capture_output=True)
        assert result.returncode == 0
