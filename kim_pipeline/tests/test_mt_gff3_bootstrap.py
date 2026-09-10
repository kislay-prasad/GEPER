"""
tests/test_mt_gff3_bootstrap.py
────────────────────────────────
Regression tests for the mitochondrial-only GFF3 auto-fetch bootstrap
(pipeline/annotation/mt_gff3_bootstrap.py) and its wiring into
AnnotationStage / pipeline.orchestration.shared. All network access is
mocked — these tests must run identically offline.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


_MT_GFF3_FIXTURE = (
    "##gff-version 3\n"
    "#!genome-build GRCh38.p14\n"
    "NC_012920.1\tRefSeq\tregion\t1\t16569\t.\t+\t.\tID=NC_012920.1:1..16569\n"
    "NC_012920.1\tRefSeq\tgene\t3230\t3304\t.\t+\t.\t"
    "ID=gene-TRNL1;Name=MT-TL1;gene=MT-TL1;Dbxref=GeneID:4567\n"
    "NC_012920.1\tRefSeq\ttRNA\t3230\t3304\t.\t+\t.\t"
    "ID=rna-TRNL1;Parent=gene-TRNL1;gene=MT-TL1\n"
)


# ─── fetch_mt_gff3 ─────────────────────────────────────────────────────────────


class TestFetchMtGff3:
    def test_fetch_success_writes_cache_and_returns_path(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import fetch_mt_gff3

        fake_resp = MagicMock(status_code=200, text=_MT_GFF3_FIXTURE)
        with patch(
            "pipeline.annotation.mt_gff3_bootstrap._api_get", return_value=fake_resp
        ) as mock_get:
            path = fetch_mt_gff3(cache_dir=str(tmp_path))
        assert path is not None
        assert os.path.exists(path)
        with open(path) as fh:
            assert "MT-TL1" in fh.read()
        mock_get.assert_called_once()

    def test_second_call_uses_disk_cache_no_network(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import fetch_mt_gff3

        fake_resp = MagicMock(status_code=200, text=_MT_GFF3_FIXTURE)
        with patch(
            "pipeline.annotation.mt_gff3_bootstrap._api_get", return_value=fake_resp
        ) as mock_get:
            fetch_mt_gff3(cache_dir=str(tmp_path))
        mock_get.assert_called_once()
        with patch("pipeline.annotation.mt_gff3_bootstrap._api_get") as mock_get2:
            path2 = fetch_mt_gff3(cache_dir=str(tmp_path))
        assert path2 is not None
        mock_get2.assert_not_called()

    def test_fetch_failure_returns_none_never_raises(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import fetch_mt_gff3

        with patch("pipeline.annotation.mt_gff3_bootstrap._api_get", return_value=None):
            path = fetch_mt_gff3(cache_dir=str(tmp_path))
        assert path is None

    def test_malformed_payload_returns_none(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import fetch_mt_gff3

        fake_resp = MagicMock(status_code=200, text="not a gff3 file")
        with patch("pipeline.annotation.mt_gff3_bootstrap._api_get", return_value=fake_resp):
            path = fetch_mt_gff3(cache_dir=str(tmp_path))
        assert path is None

    def test_http_error_status_returns_none(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import fetch_mt_gff3

        fake_resp = MagicMock(status_code=502, text="")
        with patch("pipeline.annotation.mt_gff3_bootstrap._api_get", return_value=fake_resp):
            path = fetch_mt_gff3(cache_dir=str(tmp_path))
        assert path is None


# ─── resolve_gff3_source ───────────────────────────────────────────────────────


class TestResolveGff3Source:
    def test_explicit_path_wins_and_never_fetches(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import resolve_gff3_source

        cfg = {
            "rna_analysis": {"refseq_gff": "/some/explicit/path.gff.gz", "auto_fetch_mt_gff3": True}
        }
        with patch("pipeline.annotation.mt_gff3_bootstrap.fetch_mt_gff3") as mock_fetch:
            path, provenance = resolve_gff3_source(cfg)
        assert path == "/some/explicit/path.gff.gz"
        assert provenance == "configured"
        mock_fetch.assert_not_called()

    def test_disabled_by_default_never_fetches(self):
        from pipeline.annotation.mt_gff3_bootstrap import resolve_gff3_source

        cfg = {"rna_analysis": {}}
        with patch("pipeline.annotation.mt_gff3_bootstrap.fetch_mt_gff3") as mock_fetch:
            path, provenance = resolve_gff3_source(cfg)
        assert path is None
        assert provenance == "unavailable"
        mock_fetch.assert_not_called()

    def test_enabled_via_config_triggers_bootstrap(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import resolve_gff3_source

        cfg = {"rna_analysis": {"auto_fetch_mt_gff3": True, "gff3_cache_dir": str(tmp_path)}}
        with patch(
            "pipeline.annotation.mt_gff3_bootstrap.fetch_mt_gff3",
            return_value=str(tmp_path / "x.gff3"),
        ) as mock_fetch:
            path, provenance = resolve_gff3_source(cfg)
        assert provenance == "mt_bootstrap"
        assert path == str(tmp_path / "x.gff3")
        mock_fetch.assert_called_once()

    def test_enabled_via_env_var(self, tmp_path, monkeypatch):
        from pipeline.annotation.mt_gff3_bootstrap import resolve_gff3_source

        monkeypatch.setenv("GEPER_AUTO_FETCH_MT_GFF3", "1")
        with patch(
            "pipeline.annotation.mt_gff3_bootstrap.fetch_mt_gff3",
            return_value=str(tmp_path / "x.gff3"),
        ):
            path, provenance = resolve_gff3_source({})
        assert provenance == "mt_bootstrap"

    def test_bootstrap_failure_reports_unavailable(self, tmp_path):
        from pipeline.annotation.mt_gff3_bootstrap import resolve_gff3_source

        cfg = {"rna_analysis": {"auto_fetch_mt_gff3": True}}
        with patch("pipeline.annotation.mt_gff3_bootstrap.fetch_mt_gff3", return_value=None):
            path, provenance = resolve_gff3_source(cfg)
        assert path is None
        assert provenance == "unavailable"


# ─── AnnotationStage wiring ─────────────────────────────────────────────────────


class TestAnnotationStageMtBootstrap:
    def _write_vcf(self, tmp_path, chrom="MT", pos=3243, ref="A", alt="G"):
        vcf = tmp_path / "variant.vcf"
        vcf.write_text(
            "##fileformat=VCFv4.2\n"
            f"##contig=<ID={chrom}>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
            f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t100\tPASS\t.\tGT\t1/1\n"
        )
        return str(vcf)

    def test_default_config_never_calls_network(self, tmp_path):
        """Regression: default config (auto_fetch_mt_gff3 unset/false) must
        behave exactly as before this feature existed — no network call,
        degraded-mode annotation."""
        from pipeline.annotation.stage import AnnotationStage

        vcf = self._write_vcf(tmp_path)
        out = tmp_path / "out"
        with patch("pipeline.annotation.mt_gff3_bootstrap._api_get") as mock_get:
            stage = AnnotationStage(cfg={})
            result = stage.run(vcf, str(out), sample_id="t")
        mock_get.assert_not_called()
        assert result is not None
        assert stage.gff_provenance == "unavailable"

    def test_bootstrap_resolves_gene_for_mt_variant(self, tmp_path):
        from pipeline.annotation.stage import AnnotationStage

        vcf = self._write_vcf(tmp_path)
        out = tmp_path / "out"
        fake_resp = MagicMock(status_code=200, text=_MT_GFF3_FIXTURE)
        cfg = {
            "rna_analysis": {"auto_fetch_mt_gff3": True, "gff3_cache_dir": str(tmp_path / "cache")}
        }
        with patch("pipeline.annotation.mt_gff3_bootstrap._api_get", return_value=fake_resp):
            stage = AnnotationStage(cfg=cfg)
            result = stage.run(vcf, str(out), sample_id="t")

        assert stage.gff_provenance == "mt_bootstrap"
        variants = result.variants if hasattr(result, "variants") else result["variants"]
        v = variants[0]
        gene = v.gene_name if hasattr(v, "gene_name") else v["gene_name"]
        assert gene == "MT-TL1"
        consequence = v.consequence if hasattr(v, "consequence") else v.get("consequence")
        # Must NOT be mislabeled "intergenic_variant" now that a real gene
        # resolved — see stage.py's gene_region_variant fallback.
        assert consequence != "intergenic_variant"

    def test_bootstrap_does_not_affect_nuclear_variant_lookup(self, tmp_path):
        """A GRCh38 nuclear-chromosome variant must still show as
        unresolved when only the MT bootstrap annotation is active —
        the bootstrap must never be mistaken for full-genome coverage."""
        from pipeline.annotation.stage import AnnotationStage

        vcf = self._write_vcf(tmp_path, chrom="chr17", pos=43057051, ref="A", alt="T")
        out = tmp_path / "out"
        fake_resp = MagicMock(status_code=200, text=_MT_GFF3_FIXTURE)
        cfg = {
            "rna_analysis": {"auto_fetch_mt_gff3": True, "gff3_cache_dir": str(tmp_path / "cache")}
        }
        with patch("pipeline.annotation.mt_gff3_bootstrap._api_get", return_value=fake_resp):
            stage = AnnotationStage(cfg=cfg)
            result = stage.run(vcf, str(out), sample_id="t")

        variants = result.variants if hasattr(result, "variants") else result["variants"]
        v = variants[0]
        gene = v.gene_name if hasattr(v, "gene_name") else v["gene_name"]
        assert gene is None


# ─── shared.py gene_unavailable_reason wiring ──────────────────────────────────


class TestSharedGeneUnavailableReason:
    def test_reason_mentions_mt_scope_when_bootstrap_active(self, tmp_path):
        from pipeline.orchestration import shared

        cfg = {"rna_analysis": {"auto_fetch_mt_gff3": True}, "vep": {"enabled": True}}
        with patch(
            "pipeline.annotation.mt_gff3_bootstrap.fetch_mt_gff3",
            return_value=str(tmp_path / "x.gff3"),
        ):
            results = shared.run_acmg_evidence_batch(
                ann_variants=[{"chrom": "chr17", "pos": 1, "ref": "A", "alt": "T"}],
                cfg=cfg,
            )
        assert results
        reason = results[0].get("gene_unavailable_reason", "")
        assert "mitochondrial" in reason.lower()

    def test_reason_is_no_source_configured_when_disabled(self):
        from pipeline.orchestration import shared

        cfg = {"vep": {"enabled": True}}
        results = shared.run_acmg_evidence_batch(
            ann_variants=[{"chrom": "chr17", "pos": 1, "ref": "A", "alt": "T"}],
            cfg=cfg,
        )
        assert results
        reason = results[0].get("gene_unavailable_reason", "")
        assert reason == "No GFF3/VEP annotation source configured"


# ─── GffIndex tRNA/rRNA/CDS extension ──────────────────────────────────────────


class TestGffIndexNonMrnaFeatures:
    def test_trna_gene_resolves_gene_and_transcript(self, tmp_path):
        from pipeline.annotation.gff_index import GffIndex

        gff = tmp_path / "mt.gff3"
        gff.write_text(_MT_GFF3_FIXTURE)
        idx = GffIndex.from_file(str(gff))
        gene, tx = idx.lookup("MT", 3243)
        assert gene == "MT-TL1"
        assert tx == "rna-TRNL1"

    def test_cds_only_gene_resolves_transcript(self, tmp_path):
        from pipeline.annotation.gff_index import GffIndex

        gff = tmp_path / "mt_cds.gff3"
        gff.write_text(
            "##gff-version 3\n"
            "NC_012920.1\tRefSeq\tgene\t3307\t4262\t.\t+\t.\t"
            "ID=gene-ND1;Name=MT-ND1;gene=MT-ND1\n"
            "NC_012920.1\tRefSeq\tCDS\t3307\t4262\t.\t+\t0\t"
            "ID=cds-YP_003024026.1;Parent=gene-ND1;gene=MT-ND1\n"
        )
        idx = GffIndex.from_file(str(gff))
        gene, tx = idx.lookup("MT", 3400)
        assert gene == "MT-ND1"
        assert tx == "cds-YP_003024026.1"

    def test_existing_mrna_only_behavior_unchanged(self, tmp_path):
        """Regression: files with only gene/mRNA features (the pre-existing
        supported shape) must resolve exactly as before."""
        from pipeline.annotation.gff_index import GffIndex

        gff = tmp_path / "brca1.gff3"
        gff.write_text(
            "NC_000017.11\tRefSeq\tgene\t43044295\t43125364\t.\t-\t.\t"
            "ID=gene-BRCA1;Name=BRCA1;gene=BRCA1;Dbxref=GeneID:672\n"
            "NC_000017.11\tRefSeq\tmRNA\t43044295\t43125364\t.\t-\t.\t"
            "ID=rna-NM_007294.4;Parent=gene-BRCA1;transcript_id=NM_007294.4;gene=BRCA1\n"
        )
        idx = GffIndex.from_file(str(gff))
        gene, tx = idx.lookup("chr17", 43057051)
        assert gene == "BRCA1"
        assert tx == "NM_007294.4"
