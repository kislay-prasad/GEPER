"""
Group B of T1-F2: the 6 provenance-capture sites in
`orchestrator.py::_capture_stage_provenance`.

Same defect and same fix as the 12 stage sites already landed in
85c651c: `if result.get("error"):` answers "did this lookup fail?" by
asking "is the error message non-empty?". A source that failed and
reported it as `""` -- `str(exc)` on an exception raised with no
message is the ordinary way that happens -- takes the SUCCESS branch,
and the run's provenance record then says the source answered normally.

Provenance is a RECORD, not a clinical output: no ACMG criterion, no
classification and no report line moves as a result of anything in this
file. That is exactly why this half of T1-F2 needed no ruling and could
land while :1313 and :1786 wait for counts -- see
`test_pinned_unruled_truthiness_sites.py` for those two, pinned rather
than fixed.

WHY A WRONG PROVENANCE RECORD STILL MATTERS: the whole point of the
provenance layer is to let a reader answer "which sources actually
answered when this variant was interpreted?" months later. A failure
recorded as a success is not a missing answer, it is a confident wrong
one, and there is nothing downstream that can detect it.

EVERY ASSERTION HERE IS ON THE NOTES TEXT, NOT ON THE STATUS, AND THAT
IS DELIBERATE. Writing this file the obvious way -- assert the source
was recorded UNKNOWN -- produced a test that passed before the fix as
well as after, because AlphaFold's `else:` branch (:1735) records
UNKNOWN for a SUCCESSFUL query that carried no version. Status alone
cannot tell "this query failed" from "this query succeeded and had
nothing to report"; only the note distinguishes them. The string
"Most recent query failed" appears in the error branch and nowhere
else, so it is what these tests match on.

THE SIXTH SITE IS NOT LIKE THE OTHER FIVE. :1634/:1652/:1680/:1699/:1720
each record one named source. :1768 (functional evidence) records
against BOTH ClinGen ERepo and MaveDB, because the composite provider
does not disclose which of the two failed -- so an empty-string error
there loses two records, not one. It is covered separately below.
"""

import unittest

from pipeline.orchestrator import GeperPipeline
from pipeline.provenance import VersionStatus


FAILURE_NOTE = "Most recent query failed"


class _RecordingProvenance:
    """Captures `record()` calls instead of building real provenance.

    A double rather than the real `RunProvenanceCollector` because the
    assertion here is about WHICH CALLS ARE MADE, and the real
    collector's priority ordering (UNKNOWN < TIMESTAMP_ONLY, documented
    on `_capture_stage_provenance`) would let a missing UNKNOWN record
    hide behind a later, better one from the same run.
    """

    def __init__(self):
        self.calls = []

    def record(self, source, status, **kwargs):
        self.calls.append((source, status, kwargs))

    def notes_for(self, source):
        return [kw.get("notes") or "" for src, _status, kw in self.calls if src == source]

    def recorded_a_failure_for(self, source):
        return any(FAILURE_NOTE in note for note in self.notes_for(source))

    def statuses_for(self, source):
        return [status for src, status, _kw in self.calls if src == source]


class _FakeSequenceContextGen:
    """Only `.assembly` is read, by the gnomAD branch at :1661."""

    def __init__(self, assembly):
        self.assembly = assembly


def _capture(assembly="GRCh38", **results):
    """Drive the real `_capture_stage_provenance` with a provenance double.

    Built with `object.__new__` on purpose: the method reads only
    `self.provenance` and `self.sequence_context_gen.assembly`, and
    going through `GeperPipeline.__init__` would load models and resolve
    a real assembly to test six dict lookups.

    NOTE: `_capture_stage_provenance` swallows every exception per
    source ("never raises", by design). An attribute this double failed
    to provide would therefore not error -- it would silently skip that
    source and read as a clean pass. `test_the_double_is_complete_enough`
    below is what stops that from happening quietly.
    """
    pipeline = object.__new__(GeperPipeline)
    pipeline.provenance = _RecordingProvenance()
    pipeline.sequence_context_gen = _FakeSequenceContextGen(assembly)
    kwargs = {
        "clinvar_result": {},
        "dbsnp_result": {},
        "gnomad_result": None,
        "uniprot_result": None,
        "interpro_result": None,
        "alphafold_result": None,
        "functional_evidence_result": None,
        "conservation_result": None,
    }
    kwargs.update(results)
    pipeline._capture_stage_provenance(**kwargs)
    return pipeline.provenance


# (keyword argument, recorded source name) for the five single-source sites.
#
# Five of the six sit in an elif chain BEHIND a version check, so a
# result carrying a version never reaches the error branch. A bare
# `{"error": ...}` is the minimal shape that gets there for all of them.
SITES = [
    ("clinvar_result", "ClinVar"),
    ("dbsnp_result", "dbSNP"),
    ("uniprot_result", "UniProt"),
    ("interpro_result", "InterPro"),
    ("alphafold_result", "AlphaFold DB"),
]


class TestAnEmptyErrorMessageIsStillAFailedQuery(unittest.TestCase):
    def test_every_single_source_site_records_the_failure_on_an_empty_error(self):
        """THE DANGEROUS CASE, all five single-source sites at once.

        `error: ""` means the query failed and the reason was blank --
        not that it succeeded.
        """
        for key, source in SITES:
            with self.subTest(source=source):
                provenance = _capture(**{key: {"error": ""}})
                self.assertTrue(
                    provenance.recorded_a_failure_for(source),
                    f"{source} reported a failure with an empty message and it was not recorded as a "
                    f"failure; calls were {provenance.calls!r}",
                )

    def test_functional_evidence_records_against_both_sources(self):
        """:1768 is the one site that fans out to two records.

        Asserted by name rather than by count -- a count of 2 would also
        pass if the same source were recorded twice.

        This site writes its own note text ("A functional-evidence query
        failed this run"), not the shared "Most recent query failed" the
        other five use, so it is matched on its own string. Reusing the
        shared constant here failed, which is the point of matching on
        text a passing branch cannot produce.
        """
        provenance = _capture(functional_evidence_result={"error": ""})
        for source in ("Functional evidence (ClinGen ERepo)", "Functional evidence (MaveDB)"):
            notes = provenance.notes_for(source)
            self.assertTrue(
                any("A functional-evidence query failed this run" in n for n in notes),
                f"{source} was not recorded as failed; calls were {provenance.calls!r}",
            )


class TestTheBranchesThatMustNotMove(unittest.TestCase):
    """Controls. A fix that recorded a failure whenever the key was
    merely PRESENT would pass everything above and mark every successful
    query in the run as failed."""

    def test_a_successful_result_with_no_error_key_records_no_failure(self):
        for key, source in SITES:
            with self.subTest(source=source):
                provenance = _capture(**{key: {"version": "x"}})
                self.assertFalse(
                    provenance.recorded_a_failure_for(source),
                    f"{source}: {provenance.calls!r}",
                )

    def test_an_explicit_none_error_is_not_a_failure(self):
        """`error: None` is the shape a successful stage actually emits
        (the dataclasses set the field to None), so this control is the
        one that would catch a fix written as `"error" in result`."""
        for key, source in SITES:
            with self.subTest(source=source):
                provenance = _capture(**{key: {"error": None}})
                self.assertFalse(
                    provenance.recorded_a_failure_for(source),
                    f"{source}: {provenance.calls!r}",
                )

    def test_a_real_error_message_still_records_it_and_keeps_its_text(self):
        provenance = _capture(clinvar_result={"error": "HTTP 503"})
        self.assertTrue(any("HTTP 503" in n for n in provenance.notes_for("ClinVar")), provenance.calls)

    def test_the_version_branch_still_wins_over_the_error_branch(self):
        """dbSNP records a KNOWN version when a build is present, even
        alongside an error -- pinned because the fix touches the `elif`
        this branch falls through to."""
        provenance = _capture(dbsnp_result={"detail": {"dbsnp_build": "156"}, "error": ""})
        self.assertIn(VersionStatus.VERSION_KNOWN, provenance.statuses_for("dbSNP"))
        self.assertFalse(provenance.recorded_a_failure_for("dbSNP"))

    def test_alphafold_success_with_no_version_is_still_not_a_failure(self):
        """The branch that made a status-based assertion useless, pinned
        so the distinction stays visible: this records UNKNOWN, exactly
        like the error branch, but it is a SUCCESS and its note says so."""
        provenance = _capture(alphafold_result={"found": True})
        self.assertIn(VersionStatus.UNKNOWN, provenance.statuses_for("AlphaFold DB"))
        self.assertFalse(provenance.recorded_a_failure_for("AlphaFold DB"))
        self.assertTrue(
            any("Queried successfully" in n for n in provenance.notes_for("AlphaFold DB")),
            provenance.calls,
        )

    def test_the_gnomad_site_already_fixed_is_unchanged(self):
        """:1664 was already `is not None` before this change. Pinned so
        a future sweep of this method cannot quietly revert it.

        Driven with an assembly that resolves to no dataset id, because
        the gnomAD error branch sits behind that check and is otherwise
        unreachable (GRCh38 -> gnomad_r4, which wins)."""
        provenance = _capture(assembly="GRCh00", gnomad_result={"error": ""})
        self.assertIn(VersionStatus.UNKNOWN, provenance.statuses_for("gnomAD"))

    def test_the_double_is_complete_enough(self):
        """`_capture_stage_provenance` swallows per-source exceptions by
        design, so a missing attribute on the double would look like a
        clean pass rather than an error. This drives every source at once
        and asserts each one actually recorded something -- the same
        reason the orchestrator tests assert the mock was called."""
        provenance = _capture(
            assembly="GRCh00",
            clinvar_result={"error": "e"},
            dbsnp_result={"error": "e"},
            gnomad_result={"error": "e"},
            uniprot_result={"error": "e"},
            interpro_result={"error": "e"},
            alphafold_result={"error": "e"},
            functional_evidence_result={"error": "e"},
        )
        recorded = {source for source, _s, _k in provenance.calls}
        for expected in (
            "ClinVar",
            "dbSNP",
            "gnomAD",
            "UniProt",
            "InterPro",
            "AlphaFold DB",
            "Functional evidence (ClinGen ERepo)",
            "Functional evidence (MaveDB)",
        ):
            self.assertIn(expected, recorded, f"nothing recorded for {expected}; calls {provenance.calls!r}")


if __name__ == "__main__":
    unittest.main()
