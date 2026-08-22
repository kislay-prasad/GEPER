"""
THE PROPERTY: an AlphaFold per-residue pLDDT is reported for a variant's
protein position ONLY when that position is provably the residue the
model actually predicted. Every other case -- position outside the
entry's declared UniProt span, position absent from the model, the
model's own residue numbering irreconcilable with the span it claims to
cover, no position at all -- must yield NO residue-level confidence and
a recorded reason, never a wrong-but-plausible pLDDT.

The gate's three conditions are named A (range), B (presence) and SPAN
CONTAINMENT. The third deliberately carries no letter: "condition C"
already meant the ISOFORM check in
`test_alphafold_mapping_gate_isoform.py`, and one letter meaning two
different checks in two modules helps nobody.

WHY THIS IS THE DANGEROUS DIRECTION. A pLDDT is read downstream as
structural evidence about the variant's own residue
(`confidence_engine.py:479`, `conflict_resolution_engine.py:643`,
`interpretation.py:859`) and is printed into the clinical report
(`report_generator.py:1813`, `clinical_report_builder.py:704`). A
missing pLDDT costs a piece of evidence. A pLDDT belonging to a
DIFFERENT residue asserts a confident structural claim about a residue
nobody looked at -- and it is indistinguishable, downstream, from the
real thing. Losing evidence is a gap; fabricating it is a wrong answer.

=== WHAT WAS ACTUALLY BROKEN (measured 2026-08-23, not assumed) ========

`provider.py::_build_annotation` carried TWO independent coordinate
claims and never reconciled them:

    uniprot_start / uniprot_end   <- summary.get("uniprotStart"/"uniprotEnd")
    residue_plddt keys            <- parse_pdb_plddt(<the structure file>)

and picked the residue with a bare, unguarded lookup (was line 237):

    affected_plddt = residue_plddt.get(protein_position) if (...) else None

`uniprot_start` and `uniprot_end` were stored on the annotation and
never read. Driving the REAL `_build_annotation` with an entry
declaring it covers UniProt 1201-1400 while its parsed structure
numbers residues 1..200 -- and asking for position 175 -- returned:

    found=True  uniprot_start=1201  uniprot_end=1400
    protein_position=175  affected_residue_plddt=92.0
    affected_residue_band="very_high"  protein_position_basis="transcript_cds"

An annotation that states, in its own fields, that it covers residues
1201-1400 and simultaneously reports a "very_high" confidence for
residue 175. That is not a hypothetical shape: `provider.py:158` picks
`entries[0]` with the comment "first fragment/model", so the code
already knows an entry can be one fragment of a longer protein, and
the fragment's own summary is what supplies the span.

=== WHAT WAS *NOT* BROKEN, SAID PLAINLY ================================

The dispatch asked for condition B on the grounds that "pLDDT 0.0 is
falsy and must not read as absent". Measured: `_build_annotation`
already used `.get()`, which returns 0.0 as 0.0, so a 0.0 pLDDT was
NOT being lost today. The 0.0 tests below are therefore a constraint on
the GATE -- it must ask `position in residue_plddt`, never
`residue_plddt.get(position)` in a boolean -- and a regression pin, not
the closing of an existing defect. Distinguished here rather than
folded into the defect count.

=== TWO STATES, ONE OBSERVABLE ========================================

Before the gate, "position 175 is not in this model" and "no position
was supplied" both surfaced as `affected_residue_plddt=None`, separable
only by `protein_position_basis`, and NEITHER recorded why. The gate's
`reason` is what makes the unavailable cases nameable at all.

=== SIGNATURE DEVIATION, DELIBERATE AND FLAGGED =======================

The dispatch specified `uniprot_start: int, uniprot_end: int`
(non-optional). Real data does not honour that: the local-dataset path
does `summary=record.get("summary") or {}` (`provider.py:112`), and a
record with no summary block yields `uniprot_start=None` /
`uniprot_end=None` -- measured, CASE 5 of the same probe. Two live
fixtures already take that shape
(`tests/test_alphafold_provider.py:62` and `:80`, summaries carrying
neither key). Keeping `int` would have forced every call site to decide
what a missing span means, which is exactly the scattering the dispatch
forbade. The gate therefore accepts `Optional[int]` and answers the
question itself: no declared span is not confidence, so it returns
(False, reason). Deviation reported, not silently taken.
"""

import unittest

from pipeline.alphafold.mapping_gate import check_alphafold_mapping_gate
from pipeline.alphafold.provider import _build_annotation

# A well-behaved entry: declares 1-393, models every residue in it.
FULL_SPAN = {"latestVersion": 4, "pdbUrl": "https://example.org/m.pdb", "uniprotStart": 1, "uniprotEnd": 393}
FULL_PLDDT = {i: 88.0 for i in range(1, 394)}

# A fragment entry: declares 1201-1400 but numbers its residues 1..200,
# i.e. the structure file uses fragment-local numbering. This is the
# shape that produced a "very_high" band for residue 175 before the gate.
FRAGMENT_SPAN = {"latestVersion": 4, "pdbUrl": "https://example.org/f.pdb", "uniprotStart": 1201, "uniprotEnd": 1400}
FRAGMENT_LOCAL_PLDDT = {i: 92.0 for i in range(1, 201)}


class TestConditionARange(unittest.TestCase):
    """uniprot_start <= position <= uniprot_end, inclusive at both ends."""

    def test_position_below_start_is_rejected(self):
        ok, reason = check_alphafold_mapping_gate(99, 100, 200, {99: 90.0})
        self.assertFalse(ok)
        self.assertIsNotNone(reason)
        self.assertIn("99", reason)
        self.assertIn("100", reason)

    def test_position_above_end_is_rejected(self):
        ok, reason = check_alphafold_mapping_gate(201, 100, 200, {201: 90.0})
        self.assertFalse(ok)
        self.assertIsNotNone(reason)
        self.assertIn("201", reason)

    def test_position_exactly_at_start_is_accepted(self):
        ok, reason = check_alphafold_mapping_gate(100, 100, 200, {100: 90.0})
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_position_exactly_at_end_is_accepted(self):
        ok, reason = check_alphafold_mapping_gate(200, 100, 200, {200: 90.0})
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_position_within_range_is_accepted(self):
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, {150: 90.0})
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_a_rejection_names_the_range_it_failed(self):
        """The reason has to be usable by a reader who cannot see the inputs."""
        _, reason = check_alphafold_mapping_gate(50, 100, 200, {50: 90.0})
        self.assertIn("100", reason)
        self.assertIn("200", reason)


class TestConditionBPresence(unittest.TestCase):
    """`position in residue_plddt` -- membership, never truthiness of the value."""

    def test_missing_residue_is_rejected(self):
        plddt = {i: 90.0 for i in range(100, 201) if i != 150}
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, plddt)
        self.assertFalse(ok)
        self.assertIn("150", reason)

    def test_residue_present_with_normal_plddt_is_accepted(self):
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, {150: 90.0})
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_residue_present_with_plddt_exactly_zero_is_accepted(self):
        """
        0.0 is a real, modelled, very-low-confidence residue. It is falsy.
        A gate written as `if residue_plddt.get(position):` passes every
        other test in this class and fails this one -- which is the only
        reason this test exists.
        """
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, {150: 0.0})
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_empty_residue_map_is_rejected(self):
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, {})
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_empty_residue_map_is_not_reported_as_a_range_failure(self):
        """An unfetched structure and an out-of-range position are different facts."""
        _, empty_reason = check_alphafold_mapping_gate(150, 100, 200, {})
        _, range_reason = check_alphafold_mapping_gate(50, 100, 200, {50: 90.0})
        self.assertNotEqual(empty_reason, range_reason)


class TestSpanContainment(unittest.TestCase):
    """
    Modelled residues must be a SUBSET of [start, end] -- not equal to it.
    Equality would fire on every legitimately gapped model; span
    containment catches only numbering that cannot belong to the
    declared span.

    Called "span containment", never "condition C": that letter was
    already in use for the ISOFORM check
    (`test_alphafold_mapping_gate_isoform.py`), which is a different
    check at a different stage. See the naming note in
    `pipeline/alphafold/mapping_gate.py`.
    """

    def test_all_residues_within_span_is_accepted(self):
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, {i: 90.0 for i in range(100, 201)})
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_one_residue_below_span_is_rejected(self):
        plddt = {i: 90.0 for i in range(100, 201)}
        plddt[99] = 90.0
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, plddt)
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_one_residue_above_span_is_rejected(self):
        plddt = {i: 90.0 for i in range(100, 201)}
        plddt[201] = 90.0
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, plddt)
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_gapped_model_is_accepted(self):
        """AlphaFold models have gaps. Gaps are not corruption."""
        plddt = {i: 90.0 for i in range(100, 201) if not (120 <= i <= 140)}
        ok, reason = check_alphafold_mapping_gate(150, 100, 200, plddt)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_truncated_model_is_contained_and_passes_when_position_survives(self):
        """
        Missing trailing residues are still a SUBSET of the declared span,
        so span containment holds. Truncation is caught by condition B, and only
        when the queried position is actually in the missing tail -- which
        is the correct division of labour between the two conditions.
        """
        truncated = {i: 90.0 for i in range(100, 151)}
        ok, _ = check_alphafold_mapping_gate(150, 100, 200, truncated)
        self.assertTrue(ok)

        ok_tail, reason_tail = check_alphafold_mapping_gate(180, 100, 200, truncated)
        self.assertFalse(ok_tail)
        self.assertIn("180", reason_tail)

    def test_fragment_local_numbering_is_rejected_even_when_position_is_in_range(self):
        """
        The case condition A alone cannot catch: the position falls inside
        the declared span, and the model happens to have a residue with
        that number -- but the model's numbering is fragment-local, so the
        residue is a different one. Span containment is the only condition
        that sees this.
        """
        ok, reason = check_alphafold_mapping_gate(1300, 1201, 1400, FRAGMENT_LOCAL_PLDDT)
        self.assertFalse(ok)
        self.assertIsNotNone(reason)


class TestDegradesNeverRaises(unittest.TestCase):
    """A rendering input cannot be fatal to interpretation."""

    def test_no_protein_position_is_a_reasoned_false(self):
        ok, reason = check_alphafold_mapping_gate(None, 1, 393, FULL_PLDDT)
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_missing_uniprot_span_is_a_reasoned_false(self):
        ok, reason = check_alphafold_mapping_gate(150, None, None, {150: 90.0})
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_half_a_span_is_a_reasoned_false(self):
        for start, end in ((1, None), (None, 393)):
            with self.subTest(start=start, end=end):
                ok, reason = check_alphafold_mapping_gate(150, start, end, {150: 90.0})
                self.assertFalse(ok)
                self.assertIsNotNone(reason)

    def test_inverted_span_is_a_reasoned_false(self):
        """end < start is not a range; it is a corrupt summary."""
        ok, reason = check_alphafold_mapping_gate(150, 400, 1, {150: 90.0})
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_gate_never_raises_on_hostile_input(self):
        cases = [
            (None, None, None, {}),
            (0, 0, 0, {0: 0.0}),
            (-5, -10, 10, {-5: 50.0}),
            (150, 1, 393, {}),
        ]
        for case in cases:
            with self.subTest(case=case):
                ok, reason = check_alphafold_mapping_gate(*case)
                self.assertIsInstance(ok, bool)
                self.assertTrue(reason is None or isinstance(reason, str))

    def test_a_true_verdict_never_carries_a_reason(self):
        """(True, "something") would let a consumer render a warning beside a good value."""
        ok, reason = check_alphafold_mapping_gate(150, 1, 393, FULL_PLDDT)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_a_false_verdict_always_carries_a_reason(self):
        """(False, None) is an unavailable mapping nobody can explain to a clinician."""
        cases = [
            (None, 1, 393, FULL_PLDDT),
            (150, None, None, FULL_PLDDT),
            (500, 1, 393, FULL_PLDDT),
            (150, 1, 393, {}),
            (150, 1, 393, {1000: 90.0}),
        ]
        for case in cases:
            with self.subTest(case=case):
                ok, reason = check_alphafold_mapping_gate(*case)
                self.assertFalse(ok)
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip(), "reason must not be blank")


class TestBuildAnnotationGoesThroughTheGate(unittest.TestCase):
    """
    The integration half. These are the tests that were RED against the
    shipped code -- the gate function's own tests could only fail on
    ImportError, which proves nothing about a defect.
    """

    def test_out_of_span_position_no_longer_reports_a_pLDDT(self):
        ann = _build_annotation(
            accession="P99999",
            source="alphafold_db_api",
            summary=FRAGMENT_SPAN,
            residue_plddt=FRAGMENT_LOCAL_PLDDT,
            protein_position=175,
            structure_fetched=True,
        )
        self.assertIsNone(ann.affected_residue_plddt)
        self.assertIsNone(ann.affected_residue_band)

    def test_out_of_span_position_records_why(self):
        ann = _build_annotation(
            accession="P99999",
            source="alphafold_db_api",
            summary=FRAGMENT_SPAN,
            residue_plddt=FRAGMENT_LOCAL_PLDDT,
            protein_position=175,
            structure_fetched=True,
        )
        self.assertIsNotNone(ann.mapping_unavailable_reason)
        self.assertIn("175", ann.mapping_unavailable_reason)

    def test_the_reason_survives_to_dict(self):
        """Every consumer downstream reads the dict, not the dataclass."""
        ann = _build_annotation(
            accession="P99999",
            source="alphafold_db_api",
            summary=FRAGMENT_SPAN,
            residue_plddt=FRAGMENT_LOCAL_PLDDT,
            protein_position=175,
            structure_fetched=True,
        )
        payload = ann.to_dict()
        self.assertIn("mapping_unavailable_reason", payload)
        self.assertEqual(payload["mapping_unavailable_reason"], ann.mapping_unavailable_reason)

    def test_a_rejected_mapping_still_keeps_the_rest_of_the_entry(self):
        """
        Degrade, don't discard. The model URL, version and mean pLDDT are
        properties of the ENTRY and are still true; only the residue-level
        claim is withdrawn.
        """
        ann = _build_annotation(
            accession="P99999",
            source="alphafold_db_api",
            summary=FRAGMENT_SPAN,
            residue_plddt=FRAGMENT_LOCAL_PLDDT,
            protein_position=175,
            structure_fetched=True,
        )
        self.assertTrue(ann.found)
        self.assertEqual(ann.model_version, "4")
        self.assertIsNotNone(ann.mean_plddt)
        self.assertIsNotNone(ann.mean_plddt_band)

    def test_a_good_mapping_is_unaffected(self):
        """The control. A confident mapping must STILL report its pLDDT."""
        ann = _build_annotation(
            accession="P04637",
            source="alphafold_db_api",
            summary=FULL_SPAN,
            residue_plddt=FULL_PLDDT,
            protein_position=175,
            structure_fetched=True,
        )
        self.assertEqual(ann.affected_residue_plddt, 88.0)
        self.assertEqual(ann.affected_residue_band, "confident")
        self.assertIsNone(ann.mapping_unavailable_reason)

    def test_a_good_mapping_with_zero_plddt_is_still_reported(self):
        """0.0 must reach the report as very_low, not vanish as unavailable."""
        plddt = dict(FULL_PLDDT)
        plddt[175] = 0.0
        ann = _build_annotation(
            accession="P04637",
            source="alphafold_db_api",
            summary=FULL_SPAN,
            residue_plddt=plddt,
            protein_position=175,
            structure_fetched=True,
        )
        self.assertEqual(ann.affected_residue_plddt, 0.0)
        self.assertEqual(ann.affected_residue_band, "very_low")
        self.assertIsNone(ann.mapping_unavailable_reason)

    def test_no_position_supplied_is_distinguishable_from_a_rejected_position(self):
        """
        The two-states-one-observable half. Both yield no pLDDT; a reader
        must be able to tell "we never asked" from "we asked and the model
        could not answer".
        """
        never_asked = _build_annotation(
            accession="P04637",
            source="alphafold_db_api",
            summary=FULL_SPAN,
            residue_plddt=FULL_PLDDT,
            protein_position=None,
            structure_fetched=True,
        )
        asked_and_refused = _build_annotation(
            accession="P99999",
            source="alphafold_db_api",
            summary=FRAGMENT_SPAN,
            residue_plddt=FRAGMENT_LOCAL_PLDDT,
            protein_position=175,
            structure_fetched=True,
        )
        self.assertIsNone(never_asked.affected_residue_plddt)
        self.assertIsNone(asked_and_refused.affected_residue_plddt)
        self.assertNotEqual(
            never_asked.mapping_unavailable_reason,
            asked_and_refused.mapping_unavailable_reason,
        )

    def test_local_dataset_record_without_a_summary_gets_no_residue_claim(self):
        """
        The measured CASE 5: `summary=record.get("summary") or {}` gives a
        span of None/None, and the old code still reported a pLDDT from it.
        No declared span is not confidence.
        """
        ann = _build_annotation(
            accession="P04637",
            source="local_dataset",
            summary={},
            residue_plddt={175: 91.0},
            protein_position=175,
            structure_fetched=True,
        )
        self.assertIsNone(ann.affected_residue_plddt)
        self.assertIsNone(ann.affected_residue_band)
        self.assertIsNotNone(ann.mapping_unavailable_reason)

    def test_build_annotation_does_not_raise_when_the_gate_rejects(self):
        _build_annotation(
            accession="P99999",
            source="alphafold_db_api",
            summary={},
            residue_plddt={},
            protein_position=None,
            structure_fetched=False,
        )


class TestGateIsTheOnlyPlaceTheDecisionIsMade(unittest.TestCase):
    """
    The enforcement clause: one function, every consumer through it. A
    second inline residue lookup elsewhere would not inherit the gate,
    and this suite would not notice -- so the check is on the source.
    """

    def test_provider_has_no_bare_residue_lookup_left(self):
        import inspect

        from pipeline.alphafold import provider

        source = inspect.getsource(provider)
        self.assertNotIn("residue_plddt.get(protein_position)", source)

    def test_build_annotation_actually_calls_the_gate(self):
        import inspect

        source = inspect.getsource(_build_annotation)
        self.assertIn("check_alphafold_mapping_gate", source)


if __name__ == "__main__":
    unittest.main()
