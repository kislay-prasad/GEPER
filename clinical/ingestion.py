"""
clinical/ingestion.py — the FASTQ scanner.

A scheduled, READ-ONLY sweep of `{GEPER_FASTQ_ROOT}/{org_id}/incoming/` that
records complete, stable, verified paired-end FASTQ sets as `fastq_sets` rows.

READ-ONLY IS THE FIRST PROPERTY, NOT A NICE ONE. This module never deletes,
moves, renames or writes a file. Sequencer output is the raw evidence behind a
clinical result; a scanner that tidies its input can destroy the only copy of
something that later needs re-examining. Everything it decides is recorded in
the database, and the tree is left exactly as it was found -- asserted by
`test_the_scanner_moves_and_deletes_nothing`, which compares names, sizes and
bytes before and after.

DETECTION ONLY. Nothing here invokes a pipeline. A `fastq_sets` row says "these
two files existed, complete and verified, at this moment" and nothing more.

WHAT IT DOES NOT OWN: org isolation, the audit trail, the exception vocabulary
and the SQL all belong to `DataAccess`, and this module calls into them rather
than reproducing them. `record_fastq_set` resolves the sample scoped to the
organisation and raises the existing `NotFoundError`; this module never issues
a query of its own. That is deliberate -- a second module writing its own
INSERTs would be a parallel mechanism, which is the divergence that costs more
than the duplication saves.

THE FILENAME CONTRACT IS RULED, NOT CHOSEN, AND IT IS NARROW ON PURPOSE:

    {sample_id}_{tag}_R1_001.fastq.gz
    {sample_id}_{tag}_R2_001.fastq.gz

`sample_id` IS THE UUID OF A `samples` ROW -- THE PLATFORM'S OWN IDENTIFIER --
because a lab-supplied accession in a filename is a CALLER-SUPPLIED IDENTIFIER
ARRIVING WHERE THE PLATFORM'S IDENTIFIER BELONGS. That is not an analogy, it is
a defect this repository has already fixed once: `ResolutionRequest` in
`clinical/endpoints.py` used to carry an `actor` field, and whatever the caller
typed became the record of who resolved an exception. THE FIELD WAS REMOVED
RATHER THAN VALIDATED -- a validated claim is still a claim -- and the actor is
now derived from the authenticated session. WIDENING THIS PATTERN TO ACCEPT AN
ACCESSION WOULD RE-OPEN AT THE INGESTION BOUNDARY EXACTLY WHAT THAT CHANGE
CLOSED AT THE API BOUNDARY, and it would do so without the resolution step or
the column that would make an accession mean anything.

A SITE THAT CANNOT EMIT A UUID HAS A MAPPING PROBLEM, SOLVED AT THE SITE. It is
not grounds to loosen the contract, and the mapping is an anticipated
deployment artefact rather than an edge case. If ingestion ever genuinely needs
to know a lab-local id exists, that is a resolution step with a provenance
trail -- a schema conversation, not a wider pattern. Human ruling, 2026-09-08.

`tag` is whatever the sequencer put between them (`S1_L001` in Illumina output)
and is opaque here beyond having to match across the two mates. A file that
does not match is REJECTED AND NAMED rather than ignored, because a set that
silently never appears is indistinguishable from a directory nobody wrote to.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from clinical.data_access import Clock, NotFoundError, SystemClock

# Anchored at both ends: a partial match is not a match. The read marker is
# captured rather than assumed so R1 and R2 are told apart by the filename
# itself, not by sort order.
_FASTQ_NAME = re.compile(
    r"^(?P<sample>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"_(?P<tag>.+)_R(?P<read>[12])_001\.fastq\.gz$"
)

_CHECKSUM_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ScanOutcome:
    """One decision about one candidate set, always carrying WHY.

    `reason` is a stable enum-like string for callers to branch on; `detail`
    is the human-facing specifics. NAMES, NOT COUNTS: a scan that reports "3
    rejected" tells an operator nothing they can act on, so every outcome
    carries the sample and the filename that produced it.
    """

    reason: str
    detail: str
    sample_id: Optional[str] = None
    fastq_set_id: Optional[uuid.UUID] = None


@dataclass
class ScanResult:
    org_id: uuid.UUID
    ingested: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


class FastqScanner:
    """
    Scans one organisation's incoming directory per call.

    SIZE STABILITY NEEDS MEMORY BETWEEN SCANS, so this object holds the sizes
    it saw last time. A FILE STILL BEING WRITTEN HAS A VALID NAME AND A
    READABLE PREFIX -- it will pass the name check, it may pass a gzip read of
    what has landed so far, and its checksum will be of a fragment. Without
    this check the scanner ingests half a sequencing run and everything
    downstream is confidently wrong about complete data. So a set is NEVER
    ingested on first sighting: it is deferred, and admitted only once a later
    scan sees byte-identical sizes for both mates.

    The state is per-instance and in memory, which is the right lifetime for a
    long-lived scheduled scanner and the wrong one for a process that restarts
    between scans -- a restart re-defers everything by one cycle rather than
    admitting anything early, so the failure direction is safe. Said plainly
    because it is a real limitation, not a hidden one.
    """

    def __init__(self, dao: Any, fastq_root: Path | str, clock: Clock | None = None) -> None:
        self._dao = dao
        self._root = Path(fastq_root)
        self._clock = clock or SystemClock()
        self._last_sizes: Dict[Path, int] = {}

    # ── the scan ─────────────────────────────────────────────────────────────

    def scan_org(self, org_id: uuid.UUID) -> ScanResult:
        result = ScanResult(org_id=org_id)
        incoming = self._root / str(org_id) / "incoming"
        if not incoming.is_dir():
            # Not an error: an organisation with nothing to ingest is the
            # ordinary case, and creating the directory would be a write.
            return result

        mates, off_contract = self._group(incoming)
        for name in off_contract:
            result.rejected.append(ScanOutcome(reason="filename_off_contract", detail=name))

        for (sample_str, tag), reads in sorted(mates.items()):
            r1, r2 = reads.get("1"), reads.get("2")
            if r1 is None or r2 is None:
                present = r1 or r2
                result.rejected.append(ScanOutcome(reason="incomplete_pair", detail=present.name, sample_id=sample_str))
                continue
            self._consider(result, org_id, sample_str, r1, r2)

        # Only the files seen THIS scan are remembered, so a set that
        # disappears and returns starts its stability clock again.
        self._last_sizes = {p: p.stat().st_size for p in self._existing(mates)}
        return result

    def _consider(self, result: ScanResult, org_id: uuid.UUID, sample_str: str, r1: Path, r2: Path) -> None:
        if not self._sizes_held(r1, r2):
            result.deferred.append(ScanOutcome(reason="awaiting_size_stability", detail=r1.name, sample_id=sample_str))
            return

        for path in (r1, r2):
            if not self._gzip_readable(path):
                result.rejected.append(ScanOutcome(reason="gzip_unreadable", detail=path.name, sample_id=sample_str))
                return

        r1_checksum = self._checksum(r1)
        r2_checksum = self._checksum(r2)

        existing = self._dao.find_fastq_set_by_checksums(org_id, r1_checksum, r2_checksum)
        if existing is not None:
            # The same two file contents, already recorded. Not an error and
            # not a re-ingest: the table is append-only, so there is nothing to
            # update either -- the row already says what this scan would say.
            result.skipped.append(
                ScanOutcome(reason="already_recorded", detail=r1.name, sample_id=sample_str, fastq_set_id=existing)
            )
            return

        try:
            sample_id = uuid.UUID(sample_str)
        except ValueError:
            result.rejected.append(
                ScanOutcome(reason="sample_unresolved", detail="sample not found", sample_id=sample_str)
            )
            return

        try:
            fastq_set_id = self._dao.record_fastq_set(
                org_id=org_id,
                sample_id=sample_id,
                r1_path=str(r1),
                r2_path=str(r2),
                r1_checksum=r1_checksum,
                r2_checksum=r2_checksum,
                detected_at=self._clock.now(),
            )
        except NotFoundError:
            # ANOTHER ORGANISATION'S SAMPLE AND A SAMPLE THAT DOES NOT EXIST
            # ARRIVE HERE AS THE SAME EXCEPTION AND LEAVE AS THE SAME OUTCOME,
            # with the same `reason` and the same `detail`. Reporting them
            # differently would tell the caller which sample ids exist in
            # another organisation, which is the leak `NotFoundError` was
            # written to prevent -- so this handler must never grow a branch
            # that distinguishes them.
            result.rejected.append(
                ScanOutcome(reason="sample_unresolved", detail="sample not found", sample_id=sample_str)
            )
            return

        result.ingested.append(
            ScanOutcome(reason="ingested", detail=r1.name, sample_id=sample_str, fastq_set_id=fastq_set_id)
        )

    # ── the individual checks ────────────────────────────────────────────────

    def _group(self, incoming: Path) -> Tuple[Dict[Tuple[str, str], Dict[str, Path]], list]:
        """Every entry in the directory, sorted into candidate pairs or named
        as off-contract. Nothing is skipped silently."""
        mates: Dict[Tuple[str, str], Dict[str, Path]] = {}
        off_contract = []
        for path in sorted(incoming.iterdir()):
            if not path.is_file():
                continue
            match = _FASTQ_NAME.match(path.name)
            if match is None:
                off_contract.append(path.name)
                continue
            key = (match.group("sample"), match.group("tag"))
            mates.setdefault(key, {})[match.group("read")] = path
        return mates, off_contract

    def _sizes_held(self, *paths: Path) -> bool:
        """True only if EVERY mate was seen at this exact size on the previous
        scan. A file absent from the previous scan has no held size, so a new
        set is always deferred once."""
        for path in paths:
            previous = self._last_sizes.get(path)
            if previous is None or previous != path.stat().st_size:
                return False
        return True

    def _gzip_readable(self, path: Path) -> bool:
        """Decompress to EOF. A truncated transfer has a valid name, a
        plausible size and a readable gzip HEADER -- reading the whole stream
        is what tells the difference, so this deliberately does not stop early."""
        try:
            with gzip.open(path, "rb") as handle:
                while handle.read(_CHECKSUM_CHUNK):
                    pass
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            # `zlib.error` is NOT an OSError and is what a corrupt DEFLATE
            # stream actually raises once the header has been accepted
            # ("Error -3 ... invalid code lengths set"). Omitting it let the
            # exception escape the scanner and abort the whole scan -- one bad
            # file taking every good one in the directory with it. Found by
            # test_a_corrupt_gzip_is_rejected, which is the case it describes.
            return False
        return True

    def _checksum(self, path: Path) -> str:
        """SHA-256 of the file AS IT LIES ON DISK -- the compressed bytes, not
        the decompressed stream. It has to be the thing an operator can
        reproduce with `sha256sum` against the file itself."""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHECKSUM_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _existing(self, mates: Dict[Tuple[str, str], Dict[str, Path]]):
        for reads in mates.values():
            for path in reads.values():
                if path.exists():
                    yield path
