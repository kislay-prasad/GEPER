"""
Card: an-empty-message-stage-failure-drops-out-of-the-run-errors-list.

Twelve `_run_*_stage` wrappers in `pipeline/orchestrator.py` record a
stage's failure like this:

    result = self.<client>.query_variant(...)
    if result.get("error"):                       # <-- truthiness
        errors.append(f"<Stage> stage: {result['error']}")

`errors` is the run's own account of what went wrong; it is what tells a
reader the run is not clean. An error message of `""` is falsy, so a
stage that failed with an empty message is silently omitted from it --
THE RUN THEN REPORTS ITSELF AS CLEAN WHILE A STAGE FAILED. That is not a
lesser problem than a wrong value; it is the problem that HIDES wrong
values.

`""` is not hypothetical here. Nine provider paths build their error
result with a bare `str(exc)`, and `str(exc)` is `""` for any exception
raised without a message (`ValueError()`, `KeyError()`, a bare
`ConnectionError()`):

    alphafold/provider.py:152      clingen/provider.py:323
    conservation/provider.py:246   conservation/provider.py:362
    gnomad/provider.py:251         hpo/provider.py:184
    hpo/provider.py:193            interpro/provider.py:130
    uniprot/provider.py:191

This is the same mechanism commit 9e2a3ee fixed for gnomAD, and 277f5af
for eight ACMG rule guards, and 8612e45 for `StageEvidence.from_raw` --
each time only at the sites that had surfaced. `_run_gnomad_stage`
(:2430) already carries the explicit form from that first fix and is
pinned below as the control; the other twelve were never swept.

WHAT THIS FILE ASSERTS

  A. All twelve stages, by name, via subTest -- ENUMERATED, NOT SAMPLED.
     The whole finding is that a fix landed on the sites that surfaced
     and missed the rest, so a test covering "a representative few"
     would repeat the original mistake in miniature.

     Each case also asserts THE CLIENT MOCK WAS ACTUALLY CALLED. A
     wrapper that short-circuits before reaching its client would append
     nothing and look identical to a pass -- an orphaned mock does not
     fail, it quietly stops being a mock.

  B. Controls, so the fix cannot be "append unconditionally":
       - a real error message is still recorded, once, unchanged
       - `error: None` (the success shape) records NOTHING
       - a result with no `error` key at all records NOTHING
       - `_run_gnomad_stage`, already explicit since 9e2a3ee, is
         unchanged in both directions

The distinction being kept is the one this repo keeps rediscovering:
`""` (a failure that did not explain itself) and `None` (no failure) are
different states, and only `is not None` tells them apart.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from pipeline.orchestrator import GeperPipeline


def _variant():
    return SimpleNamespace(chrom="17", pos=43057051, ref="A", alt="T", build="GRCh38", gene="BRCA1")


# (stage method, client attribute, client method, positional args before `errors`)
# One row per site. Adding a stage wrapper without adding it here is the
# omission this file exists to prevent, so the row list is the coverage claim.
STAGES = [
    ("_run_indigenomes_stage", "indigenomes_client", "query_variant", lambda: (_variant(),)),
    (
        "_run_thousand_genomes_sas_stage",
        "thousand_genomes_sas_client",
        "query_variant",
        lambda: (_variant(), {"rsid": "rs80357906", "found": True}),
    ),
    ("_run_conservation_stage", "conservation_client", "query_variant", lambda: (_variant(),)),
    ("_run_clingen_stage", "clingen_client", "query_variant", lambda: (_variant(),)),
    ("_run_hpo_stage", "hpo_client", "query_variant", lambda: ({"gene_symbol": "BRCA1"},)),
    ("_run_orphanet_stage", "orphanet_client", "query_variant", lambda: ({"gene_symbol": "BRCA1"},)),
    (
        "_run_transcript_stage",
        "transcript_client",
        "query_variant",
        lambda: (_variant(), {"gene_symbol": "BRCA1"}),
    ),
    (
        "_run_clinvar_codon_stage",
        "clinvar_codon_client",
        "query_codon",
        lambda: (_variant(), {"found": True, "transcript": {"transcript_id": "ENST00000357654"}}),
    ),
    (
        "_run_functional_evidence_stage",
        "functional_evidence_client",
        "query_variant",
        lambda: (_variant(), {"gene_symbol": "BRCA1"}, {"found": True}),
    ),
    (
        "_run_uniprot_stage",
        "uniprot_client",
        "query_variant",
        lambda: (_variant(), {"gene_symbol": "BRCA1"}),
    ),
    (
        "_run_interpro_stage",
        "interpro_client",
        "query_variant",
        lambda: ({"found": True, "accession": "P38398"}, 100),
    ),
    (
        "_run_alphafold_stage",
        "alphafold_client",
        "query_variant",
        lambda: ({"found": True, "accession": "P38398"}, 100),
    ),
]


def _pipeline():
    """A GeperPipeline with only the attributes these wrappers touch.

    `__new__` rather than `__init__`: constructing a real pipeline pulls
    in every client, model loader and config this test has no interest
    in, and the wrappers under test read exactly two things off `self`
    besides their own client.
    """
    p = GeperPipeline.__new__(GeperPipeline)
    p.enable_profiling = False  # `_timer` then returns a nullcontext
    p.sequence_context_gen = SimpleNamespace(assembly="GRCh38")
    return p


# `_run_clinvar_codon_stage` resolves a real transcript object and asks it
# for the variant's codon before it ever reaches its client, and returns
# early if either is None. That dependency is not what this file is
# testing, so it is stubbed -- but only after the orphaned-mock assertion
# caught it: with a plausible-looking `transcript_result` dict the wrapper
# short-circuited, the client was never called, and the case looked like a
# clean pass in the control class. A mock nobody calls does not fail.
_STUB_TRANSCRIPT = SimpleNamespace(codon_at=lambda pos: 42)


def _run_stage(method, client_attr, client_method, args, client_result):
    p = _pipeline()
    client = mock.MagicMock()
    getattr(client, client_method).return_value = client_result
    setattr(p, client_attr, client)
    errors: list = []
    # `to_hgvs_c` is stubbed for the same reason and with the same safety
    # net: HGVS formatting is not what this file tests, and if stubbing it
    # ever caused a wrapper to short-circuit before its client, the
    # `called.called` assertion below would fail rather than pass quietly.
    with (
        mock.patch("pipeline.orchestrator.transcript_from_result", return_value=_STUB_TRANSCRIPT),
        mock.patch("pipeline.orchestrator.to_hgvs_c", return_value="c.68_69del"),
    ):
        getattr(p, method)(*args, errors)
    return errors, getattr(client, client_method)


class TestAnEmptyMessageStageFailureIsStillRecorded(unittest.TestCase):
    def test_every_stage_records_a_failure_whose_message_is_empty(self):
        """THE DANGEROUS CASE, for all twelve sites by name.

        A provider that fails with `str(exc) == ""` still failed. The run
        must not describe itself as clean.
        """
        for method, attr, cmeth, argf in STAGES:
            with self.subTest(stage=method):
                errors, called = _run_stage(
                    method, attr, cmeth, argf(), {"found": False, "skipped": False, "error": ""}
                )
                self.assertTrue(
                    called.called,
                    f"{method} never reached its client -- the mock was orphaned, so a pass here would mean nothing",
                )
                self.assertEqual(
                    len(errors),
                    1,
                    f"{method} dropped a failed stage from the run's errors list because the "
                    f"error message was empty; the run would report itself clean. errors={errors!r}",
                )

    def test_the_recorded_entry_names_the_stage_even_with_no_message(self):
        """Recording it is not enough on its own -- an entry that is just
        a bare prefix and nothing else still has to identify WHICH stage
        failed, or the reader learns only that something did."""
        errors, _ = _run_stage(
            "_run_uniprot_stage",
            "uniprot_client",
            "query_variant",
            (_variant(), {"gene_symbol": "BRCA1"}),
            {"found": False, "skipped": False, "error": ""},
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("UniProt", errors[0])


class TestTheControlsThatKeepTheFixHonest(unittest.TestCase):
    """A fix that appended unconditionally would pass the class above and
    fill every clean run's error list with phantom failures."""

    def test_a_real_error_message_is_still_recorded_once_and_unchanged(self):
        for method, attr, cmeth, argf in STAGES:
            with self.subTest(stage=method):
                errors, _ = _run_stage(
                    method,
                    attr,
                    cmeth,
                    argf(),
                    {"found": False, "skipped": False, "error": "connection reset by peer"},
                )
                self.assertEqual(len(errors), 1)
                self.assertIn("connection reset by peer", errors[0])

    def test_an_explicit_none_error_records_nothing(self):
        """`error: None` is the shape every provider uses for success. It
        must stay silent -- this is the assertion that stops the fix from
        being `errors.append` with no guard at all."""
        for method, attr, cmeth, argf in STAGES:
            with self.subTest(stage=method):
                errors, _ = _run_stage(method, attr, cmeth, argf(), {"found": True, "skipped": False, "error": None})
                self.assertEqual(errors, [], f"{method} invented a failure for a successful stage")

    def test_a_result_with_no_error_key_records_nothing(self):
        for method, attr, cmeth, argf in STAGES:
            with self.subTest(stage=method):
                errors, _ = _run_stage(method, attr, cmeth, argf(), {"found": True, "skipped": False})
                self.assertEqual(errors, [], f"{method} invented a failure from a missing key")


class TestTheStageAlreadyFixedIsUnchanged(unittest.TestCase):
    """`_run_gnomad_stage` got the explicit form in 9e2a3ee. It is the
    control for the whole file: whatever this change does to the other
    twelve, the one that was already correct must behave identically
    before and after."""

    def _gnomad(self, client_result):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.return_value = client_result
        p.gnomad_client = client
        errors: list = []
        p._run_gnomad_stage(_variant(), errors)
        return errors

    def test_empty_message_is_recorded(self):
        errors = self._gnomad({"found": False, "skipped": False, "error": ""})
        self.assertEqual(len(errors), 1, "the already-fixed stage must keep recording this")

    def test_none_records_nothing(self):
        self.assertEqual(self._gnomad({"found": True, "skipped": False, "error": None}), [])


if __name__ == "__main__":
    unittest.main()
