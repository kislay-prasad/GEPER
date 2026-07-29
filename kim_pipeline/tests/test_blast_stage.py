"""
tests/test_blast_stage.py
──────────────────────────
Unit and integration tests for pipeline/blast/stage.py.

Tests cover:
  - is_available() detection
  - BLASTNotInstalledError when binary missing
  - BLASTDatabaseError when db files missing
  - XML parser with synthetic BLAST XML
  - Tabular parser with synthetic outfmt 6 output
  - FASTA writing helper
  - BLASTResult serialisation
  - run() graceful failure when BLAST is not installed
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.blast.stage import (
    BLASTStage,
    BLASTResult,
    BLASTHit,
    BLASTError,
    BLASTNotInstalledError,
    BLASTDatabaseError,
    _parse_blast_xml,
    _parse_blast_tabular,
    _write_fasta,
)


# ─── Synthetic BLAST XML fixture ──────────────────────────────────────────────

_SAMPLE_XML = textwrap.dedent("""\
<?xml version="1.0"?>
<!DOCTYPE BlastOutput PUBLIC "-//NCBI//NCBI BlastOutput//EN" "NCBI_BlastOutput.dtd">
<BlastOutput>
  <BlastOutput_iterations>
    <Iteration>
      <Iteration_query-def>seq1 test sequence</Iteration_query-def>
      <Iteration_query-len>50</Iteration_query-len>
      <Iteration_hits>
        <Hit>
          <Hit_id>gi|12345|ref|NC_000001.11|</Hit_id>
          <Hit_def>Homo sapiens chromosome 1</Hit_def>
          <Hit_len>248956422</Hit_len>
          <Hit_hsps>
            <Hsp>
              <Hsp_bit-score>95.6</Hsp_bit-score>
              <Hsp_score>52</Hsp_score>
              <Hsp_evalue>1e-25</Hsp_evalue>
              <Hsp_query-from>1</Hsp_query-from>
              <Hsp_query-to>50</Hsp_query-to>
              <Hsp_hit-from>1000</Hsp_hit-from>
              <Hsp_hit-to>1049</Hsp_hit-to>
              <Hsp_align-len>50</Hsp_align-len>
              <Hsp_identity>49</Hsp_identity>
              <Hsp_gaps>0</Hsp_gaps>
              <Hsp_midline>XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX</Hsp_midline>
            </Hsp>
          </Hit_hsps>
        </Hit>
      </Iteration_hits>
    </Iteration>
    <Iteration>
      <Iteration_query-def>seq2 another sequence</Iteration_query-def>
      <Iteration_query-len>30</Iteration_query-len>
      <Iteration_hits/>
    </Iteration>
  </BlastOutput_iterations>
</BlastOutput>
""")

_SAMPLE_TABULAR = textwrap.dedent("""\
# BLASTN output
seq1\tgi|12345|\t98.00\t50\t1\t0\t1\t50\t1000\t1049\t1e-25\t95.6
seq3\tgi|99999|\t85.00\t30\t4\t1\t1\t30\t5000\t5029\t1e-10\t55.2
""")


# ─── Parser tests ─────────────────────────────────────────────────────────────

class TestBLASTXMLParser:

    def test_parses_hit_count(self):
        hits = _parse_blast_xml(_SAMPLE_XML)
        assert len(hits) == 1  # seq2 has no hits

    def test_parses_query_id(self):
        hits = _parse_blast_xml(_SAMPLE_XML)
        assert hits[0].query_id == "seq1"

    def test_parses_subject_title(self):
        hits = _parse_blast_xml(_SAMPLE_XML)
        assert "Homo sapiens" in hits[0].subject_title

    def test_parses_pct_identity(self):
        hits = _parse_blast_xml(_SAMPLE_XML)
        assert abs(hits[0].pct_identity - 98.0) < 0.1  # 49/50 * 100

    def test_parses_evalue(self):
        hits = _parse_blast_xml(_SAMPLE_XML)
        assert hits[0].evalue == 1e-25

    def test_parses_bitscore(self):
        hits = _parse_blast_xml(_SAMPLE_XML)
        assert hits[0].bitscore == 95.6

    def test_parses_alignment_coords(self):
        hits = _parse_blast_xml(_SAMPLE_XML)
        assert hits[0].query_start == 1
        assert hits[0].query_end == 50
        assert hits[0].subject_start == 1000
        assert hits[0].subject_end == 1049

    def test_empty_xml_returns_empty_list(self):
        xml = '<?xml version="1.0"?><BlastOutput><BlastOutput_iterations></BlastOutput_iterations></BlastOutput>'
        hits = _parse_blast_xml(xml)
        assert hits == []

    def test_malformed_xml_raises_blast_error(self):
        with pytest.raises(BLASTError):
            _parse_blast_xml("this is not xml at all")


class TestBLASTTabularParser:

    def test_parses_hit_count(self):
        hits = _parse_blast_tabular(_SAMPLE_TABULAR)
        assert len(hits) == 2

    def test_parses_query_id(self):
        hits = _parse_blast_tabular(_SAMPLE_TABULAR)
        assert hits[0].query_id == "seq1"
        assert hits[1].query_id == "seq3"

    def test_parses_pct_identity(self):
        hits = _parse_blast_tabular(_SAMPLE_TABULAR)
        assert hits[0].pct_identity == 98.0

    def test_parses_evalue(self):
        hits = _parse_blast_tabular(_SAMPLE_TABULAR)
        assert hits[0].evalue == 1e-25

    def test_parses_bitscore(self):
        hits = _parse_blast_tabular(_SAMPLE_TABULAR)
        assert hits[0].bitscore == 95.6

    def test_comment_lines_skipped(self):
        tab = "# header line\nseq1\tsubject\t99\t100\t0\t0\t1\t100\t1\t100\t1e-10\t88.0\n"
        hits = _parse_blast_tabular(tab)
        assert len(hits) == 1

    def test_short_line_skipped(self):
        tab = "seq1\tsubject\t99\n"
        hits = _parse_blast_tabular(tab)
        assert hits == []

    def test_empty_input_returns_empty(self):
        assert _parse_blast_tabular("") == []


# ─── FASTA writer tests ───────────────────────────────────────────────────────

class TestWriteFasta:

    def test_writes_fasta_format(self, tmp_path):
        p = tmp_path / "out.fasta"
        _write_fasta({"seq1": "ACGTACGT", "seq2": "TTTTGGGG"}, p)
        text = p.read_text()
        assert ">seq1" in text
        assert "ACGTACGT" in text
        assert ">seq2" in text

    def test_wraps_long_sequence(self, tmp_path):
        p = tmp_path / "out.fasta"
        long_seq = "A" * 200
        _write_fasta({"longseq": long_seq}, p)
        lines = p.read_text().splitlines()
        # Header + at least 3 wrapped lines (80, 80, 40)
        assert len(lines) >= 3

    def test_empty_dict_produces_empty_file(self, tmp_path):
        p = tmp_path / "empty.fasta"
        _write_fasta({}, p)
        assert p.read_text() == ""


# ─── BLASTStage unit tests ────────────────────────────────────────────────────

class TestBLASTStageNoInstall:
    """Tests that don't require BLAST+ to be installed."""

    def test_is_available_returns_bool(self):
        stage = BLASTStage(cfg={})
        result = stage.is_available()
        assert isinstance(result, bool)

    def test_missing_binary_raises_not_installed(self):
        stage = BLASTStage(cfg={"blast": {"blast_bin_dir": "/nonexistent/bin"}})
        with pytest.raises(BLASTNotInstalledError):
            stage.run({"seq1": "ACGT"}, db_path="/fake/db")

    def test_missing_db_raises_db_error(self, tmp_path):
        """Even with binary present, a missing db raises BLASTDatabaseError."""
        stage = BLASTStage(cfg={})
        if not stage.is_available():
            pytest.skip("blastn not installed — skipping database error test")
        with pytest.raises(BLASTDatabaseError):
            stage.run({"seq1": "ACGT"}, db_path="/nonexistent/database/prefix")

    def test_blast_result_serialisable(self):
        r = BLASTResult(
            db_path="/data/nt",
            blast_version="blastn 2.14.0+",
            query_count=2,
            hit_count=1,
            hits=[BLASTHit(query_id="seq1", bitscore=55.0, evalue=1e-10)],
            no_hit_queries=["seq2"],
        )
        d = r.to_dict()
        import json
        json.dumps(d)  # must be JSON-serialisable
        assert d["hit_count"] == 1
        assert d["hits"][0]["query_id"] == "seq1"

    def test_blast_hit_to_dict(self):
        h = BLASTHit(query_id="q1", subject_title="Homo sapiens", pct_identity=99.5)
        d = h.to_dict()
        assert d["query_id"] == "q1"
        assert d["pct_identity"] == 99.5

    def test_not_installed_error_message(self):
        exc = BLASTNotInstalledError("/opt/blast/bin")
        assert "BLAST+" in str(exc)
        assert "conda" in str(exc)

    def test_database_error_message(self):
        exc = BLASTDatabaseError("/data/nt")
        assert "/data/nt" in str(exc)
        assert "makeblastdb" in str(exc)

    def test_stage_config_read(self):
        cfg = {
            "blast": {
                "db_path": "/data/nt",
                "evalue": 1e-10,
                "max_target_seqs": 5,
                "timeout": 60,
                "output_format": "tabular",
            }
        }
        stage = BLASTStage(cfg=cfg)
        assert stage._db_path == "/data/nt"
        assert stage._evalue == 1e-10
        assert stage._max_target_seqs == 5
        assert stage._timeout == 60
        assert stage._output_format == "tabular"

    def test_run_raises_not_installed_not_on_path(self, tmp_path, monkeypatch):
        """Monkeypatch shutil.which to simulate missing binary."""
        import shutil as _shutil
        original_which = _shutil.which

        def _mock_which(name):
            if name == "blastn":
                return None
            return original_which(name)

        monkeypatch.setattr("pipeline.blast.stage.shutil.which", _mock_which)

        stage = BLASTStage(cfg={"blast": {"blast_bin_dir": ""}})
        with pytest.raises(BLASTNotInstalledError):
            stage.run({"seq1": "ACGT"}, db_path="/fake/db")


# ─── Integration test (skipped if BLAST not installed) ───────────────────────

@pytest.mark.skipif(
    not BLASTStage().is_available(),
    reason="blastn not installed",
)
class TestBLASTStageIntegration:
    """These tests run only when BLAST+ is installed."""

    def test_run_missing_db_raises(self):
        stage = BLASTStage(cfg={})
        with pytest.raises(BLASTDatabaseError):
            stage.run({"seq1": "ACGT" * 10}, db_path="/definitely/not/a/db")
