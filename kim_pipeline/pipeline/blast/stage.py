"""
pipeline/blast/stage.py
────────────────────────
Real BLAST+ integration — BLASTN (DNA sequences) against local databases.

NO mocks. NO placeholders.

Behaviour when BLAST is not installed:
  BLASTStage.run() raises BLASTNotInstalledError with a clear message
  describing how to install BLAST+. The pipeline continues gracefully
  if the caller catches this error (the orchestration runner does so by
  default).

Behaviour when the database path is missing or invalid:
  Raises BLASTDatabaseError.

Design:
  - One subprocess call per query batch (avoids repeated startup overhead).
  - BLAST XML output format (outfmt 5) is parsed with stdlib xml.etree.
  - Tabular format (outfmt 6) is also supported and is faster for large queries.
  - Per-query timeout enforced.
  - Results are returned as structured BLASTHit / BLASTResult objects.
  - No automatic database downloading — the database must be pre-built with
    makeblastdb and its path provided in configuration.

Configuration (config/default.yaml → blast section)::

    blast:
      enabled: true
      db_path: "/data/blast/nt"          # local BLAST database prefix
      blast_bin_dir: ""                   # directory of blastn binary (optional)
      evalue: 1e-5                        # E-value threshold
      max_target_seqs: 10                 # top hits to return
      word_size: 11                       # BLASTN word size
      timeout: 120                        # per-run timeout in seconds
      output_format: "xml"               # "xml" or "tabular"
      threads: 1

Usage::

    from pipeline.blast.stage import BLASTStage

    stage = BLASTStage(cfg={"blast": {"db_path": "/data/blast/nt"}})
    result = stage.run(sequences={"seq1": "ACGTACGT..."})
    for hit in result.hits:
        print(hit.query_id, hit.subject_title, hit.bitscore)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from pipeline.utils.process_control import kill_process_tree_now, spawn_tracked

logger = logging.getLogger("geper.pipeline.blast.stage")

# ─── Errors ───────────────────────────────────────────────────────────────────


class BLASTError(Exception):
    """General BLAST stage failure."""


class BLASTNotInstalledError(BLASTError):
    """BLAST+ binaries not found on PATH or configured bin_dir."""

    def __init__(self, bin_dir: str = "") -> None:
        loc = f" (searched bin_dir={bin_dir!r})" if bin_dir else ""
        super().__init__(
            f"BLAST+ not installed or not on PATH{loc}. "
            "Install via: conda install -c bioconda blast  "
            "or https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST/"
        )


class BLASTDatabaseError(BLASTError):
    """Local BLAST database not found or invalid."""

    def __init__(self, db_path: str) -> None:
        super().__init__(
            f"BLAST database not found at {db_path!r}. "
            "Build it with: makeblastdb -in sequences.fasta -dbtype nucl -out <prefix>"
        )


class BLASTTimeoutError(BLASTError):
    """BLAST run exceeded the configured timeout."""


# ─── Data models ──────────────────────────────────────────────────────────────


@dataclass
class BLASTHit:
    """Single high-scoring pair (HSP) hit from BLAST."""

    query_id: str = ""
    query_length: int = 0
    subject_id: str = ""
    subject_title: str = ""
    subject_length: int = 0
    # HSP-level metrics (best HSP per hit)
    pct_identity: float = 0.0
    alignment_length: int = 0
    mismatches: int = 0
    gap_opens: int = 0
    query_start: int = 0
    query_end: int = 0
    subject_start: int = 0
    subject_end: int = 0
    evalue: float = 0.0
    bitscore: float = 0.0
    score: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class BLASTResult:
    """Aggregated result of a BLASTStage.run() call."""

    db_path: str = ""
    blast_version: str = ""
    query_count: int = 0
    hit_count: int = 0
    hits: List[BLASTHit] = field(default_factory=list)
    no_hit_queries: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    command: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


# ─── XML parser ───────────────────────────────────────────────────────────────


def _parse_blast_xml(xml_text: str) -> List[BLASTHit]:
    """Parse BLAST XML output (outfmt 5) into BLASTHit objects."""
    hits: List[BLASTHit] = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise BLASTError(f"BLAST XML parse error: {exc}") from exc

    for iteration in root.iter("Iteration"):
        query_def_el = iteration.find("Iteration_query-def")
        query_def = (
            query_def_el.text.strip()
            if query_def_el is not None and query_def_el.text
            else "unknown"
        )
        query_id = query_def.split()[0]

        query_len_el = iteration.find("Iteration_query-len")
        query_length = (
            int(query_len_el.text) if query_len_el is not None and query_len_el.text else 0
        )

        for hit_el in iteration.iter("Hit"):
            hit_id_el = hit_el.find("Hit_id")
            hit_def_el = hit_el.find("Hit_def")
            hit_len_el = hit_el.find("Hit_len")

            subject_id = (hit_id_el.text or "").strip() if hit_id_el is not None else ""
            subject_title = (hit_def_el.text or "").strip() if hit_def_el is not None else ""
            subject_length = (
                int(hit_len_el.text) if hit_len_el is not None and hit_len_el.text else 0
            )

            # Take the first (best) HSP
            for hsp_el in hit_el.iter("Hsp"):

                def _int(tag: str) -> int:
                    el = hsp_el.find(tag)
                    return int(el.text) if el is not None and el.text else 0

                def _float(tag: str) -> float:
                    el = hsp_el.find(tag)
                    return float(el.text) if el is not None and el.text else 0.0

                aln_len = _int("Hsp_align-len")
                identity = _int("Hsp_identity")
                pct_id = (identity / aln_len * 100) if aln_len else 0.0

                hits.append(
                    BLASTHit(
                        query_id=query_id,
                        query_length=query_length,
                        subject_id=subject_id,
                        subject_title=subject_title,
                        subject_length=subject_length,
                        pct_identity=round(pct_id, 2),
                        alignment_length=aln_len,
                        mismatches=aln_len - identity,
                        gap_opens=_int("Hsp_gaps"),
                        query_start=_int("Hsp_query-from"),
                        query_end=_int("Hsp_query-to"),
                        subject_start=_int("Hsp_hit-from"),
                        subject_end=_int("Hsp_hit-to"),
                        evalue=_float("Hsp_evalue"),
                        bitscore=_float("Hsp_bit-score"),
                        score=_float("Hsp_score"),
                    )
                )
                break  # only best HSP per hit

    return hits


# ─── Tabular parser (outfmt 6) ────────────────────────────────────────────────
# qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore


def _parse_blast_tabular(tab_text: str) -> List[BLASTHit]:
    """Parse BLAST tabular output (outfmt 6) into BLASTHit objects."""
    hits: List[BLASTHit] = []
    for line in tab_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 12:
            logger.warning("Ignoring short tabular BLAST line: %s", line[:80])
            continue
        hits.append(
            BLASTHit(
                query_id=parts[0],
                subject_id=parts[1],
                subject_title=parts[1],
                pct_identity=float(parts[2]),
                alignment_length=int(parts[3]),
                mismatches=int(parts[4]),
                gap_opens=int(parts[5]),
                query_start=int(parts[6]),
                query_end=int(parts[7]),
                subject_start=int(parts[8]),
                subject_end=int(parts[9]),
                evalue=float(parts[10]),
                bitscore=float(parts[11]),
            )
        )
    return hits


# ─── FASTA writer ─────────────────────────────────────────────────────────────


def _write_fasta(sequences: Dict[str, str], path: Path) -> None:
    """Write a dict of {id: sequence} to a FASTA file."""
    with open(path, "w") as fh:
        for seq_id, seq in sequences.items():
            # Wrap at 80 bp
            fh.write(f">{seq_id}\n")
            for i in range(0, len(seq), 80):
                fh.write(seq[i : i + 80] + "\n")


# ─── BLAST stage ──────────────────────────────────────────────────────────────


class BLASTStage:
    """Real BLASTN integration against a local BLAST+ database.

    No mocks. Fails loudly if BLAST+ is not installed.

    Args:
        cfg: Full pipeline configuration dict. Reads cfg['blast'] sub-section.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        self._cfg = cfg or {}
        blast_cfg = self._cfg.get("blast", {})

        self._db_path: str = blast_cfg.get("db_path", "")
        self._bin_dir: str = blast_cfg.get("blast_bin_dir", "")
        self._evalue: float = float(blast_cfg.get("evalue", 1e-5))
        self._max_target_seqs: int = int(blast_cfg.get("max_target_seqs", 10))
        self._word_size: int = int(blast_cfg.get("word_size", 11))
        self._timeout: int = int(blast_cfg.get("timeout", 120))
        self._output_format: str = blast_cfg.get("output_format", "xml").lower()
        self._threads: int = int(blast_cfg.get("threads", 1))

    # ── Public API ──────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if the blastn binary is reachable."""
        return self._find_binary() is not None

    def run(
        self,
        sequences: Dict[str, str],
        db_path: Optional[str] = None,
        sample_id: str = "SAMPLE",
    ) -> BLASTResult:
        """Run BLASTN on the provided sequences against the configured database.

        Args:
            sequences:  Dict mapping sequence ID → DNA sequence string.
            db_path:    Override the configured database path.
            sample_id:  Sample identifier for logging.

        Returns:
            BLASTResult with populated hits list.

        Raises:
            BLASTNotInstalledError: blastn not found.
            BLASTDatabaseError:     Database path invalid.
            BLASTTimeoutError:      Run exceeded timeout.
            BLASTError:             Any other failure.
        """
        t0 = time.time()
        blastn = self._find_binary()
        if blastn is None:
            raise BLASTNotInstalledError(self._bin_dir)

        effective_db = db_path or self._db_path
        if not effective_db:
            raise BLASTDatabaseError("<not configured>")
        self._verify_database(effective_db)

        blast_version = self._get_version(blastn)
        logger.info(
            "[%s] BLAST Stage: %s seqs → db=%s (version=%s)",
            sample_id,
            len(sequences),
            effective_db,
            blast_version,
        )

        with tempfile.TemporaryDirectory(prefix="geper_blast_") as tmpdir:
            query_fasta = Path(tmpdir) / "query.fasta"
            out_file = Path(tmpdir) / "blast_out"
            _write_fasta(sequences, query_fasta)

            outfmt, use_xml = self._outfmt_args()
            cmd = [
                blastn,
                "-query",
                str(query_fasta),
                "-db",
                effective_db,
                "-out",
                str(out_file),
                "-outfmt",
                outfmt,
                "-evalue",
                str(self._evalue),
                "-max_target_seqs",
                str(self._max_target_seqs),
                "-word_size",
                str(self._word_size),
                "-num_threads",
                str(self._threads),
                "-dust",
                "no",  # disable masking for short-read genomic sequences
            ]
            logger.debug("[%s] BLAST cmd: %s", sample_id, " ".join(cmd))

            # Spawned via spawn_tracked (not a bare subprocess.run) so a
            # BLAST search in progress -- which can run for minutes against
            # a large database -- is reachable by DELETE, same as every
            # other stage's subprocess. See pipeline/utils/process_control.py.
            proc = spawn_tracked(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _stdout, stderr = proc.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                kill_process_tree_now(proc)
                try:
                    proc.communicate(timeout=1)
                except Exception:
                    pass
                raise BLASTTimeoutError(
                    f"BLAST timed out after {self._timeout}s for {len(sequences)} sequences"
                )

            if proc.returncode != 0:
                raise BLASTError(f"blastn exited {proc.returncode}:\n{(stderr or '')[:2000]}")

            out_text = out_file.read_text(errors="replace") if out_file.exists() else ""

        # Parse results
        if use_xml:
            hits = _parse_blast_xml(out_text)
        else:
            hits = _parse_blast_tabular(out_text)

        # Identify queries with no hits
        queried = set(sequences.keys())
        hit_queries = {h.query_id for h in hits}
        no_hit = sorted(queried - hit_queries)

        result = BLASTResult(
            db_path=effective_db,
            blast_version=blast_version,
            query_count=len(sequences),
            hit_count=len(hits),
            hits=hits,
            no_hit_queries=no_hit,
            elapsed_seconds=round(time.time() - t0, 2),
            command=" ".join(cmd),
        )
        logger.info(
            "[%s] BLAST done in %.1fs — %d hits for %d queries (%d no-hit)",
            sample_id,
            result.elapsed_seconds,
            len(hits),
            len(sequences),
            len(no_hit),
        )
        return result

    # ── Private helpers ─────────────────────────────────────────────────────

    def _find_binary(self) -> Optional[str]:
        """Locate the blastn binary. Returns path string or None."""
        if self._bin_dir:
            candidate = Path(self._bin_dir) / "blastn"
            if candidate.exists():
                return str(candidate)
            # Try with .exe on Windows
            candidate_exe = Path(self._bin_dir) / "blastn.exe"
            if candidate_exe.exists():
                return str(candidate_exe)
        return shutil.which("blastn")

    def _get_version(self, binary: str) -> str:
        """Return the BLAST+ version string."""
        try:
            proc = subprocess.run(
                [binary, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in proc.stdout.splitlines():
                if "blastn" in line.lower():
                    return line.strip()
            return proc.stdout.strip()[:80]
        except Exception:
            return "unknown"

    def _verify_database(self, db_path: str) -> None:
        """Check that at least one BLAST database file exists for db_path prefix.

        BLAST databases consist of files like <prefix>.nin, <prefix>.nhr, etc.
        """
        prefix = Path(db_path)
        # Check common extensions for nucleotide databases
        extensions = [".nin", ".nhr", ".nsq", ".nal"]
        found = any((Path(str(prefix) + ext)).exists() for ext in extensions)
        if not found:
            raise BLASTDatabaseError(db_path)

    def _outfmt_args(self) -> tuple[str, bool]:
        """Return (outfmt string for -outfmt, is_xml bool)."""
        if self._output_format == "xml":
            return "5", True
        # Tabular with standard 12 columns
        return "6", False
