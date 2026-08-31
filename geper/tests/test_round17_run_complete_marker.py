"""
Round 17: the checkpoint write recorded model status before it existed.

Confirmed against source (not assumed) before building: `model_checkpoints`
is enriched with per-model run status EXACTLY ONCE, in
`pipeline/orchestrator.py::run()`, via `rollup_run_status` +
`finalize_model_checkpoint_provenance` -- strictly AFTER the entire
per-variant loop finishes. The periodic in-loop checkpoint write
(`result_builder.write(json_path)`, gated on `CONFIG.CHECKPOINT_INTERVAL`)
happens BEFORE that enrichment, inside the loop. So a `geper_results.json`
left behind by a run that dies mid-loop (a real, twice-observed failure
mode on free-tier Colab: idle disconnects, quota exhaustion, and a
~45-minute cold bootstrap before variant 1 completes) shows bare
`model_checkpoints` identifiers for every model, structurally
indistinguishable from a genuinely completed run's manifest -- exactly
`ROUND_CANDIDATES.md`'s round-12 finding, confirmed still present and
unfixed (round 12 investigated, decided a shape, and explicitly deferred
building it).

This round implements round 12's own decided shape (confirmed still
correct -- see this file's module-level tests below for why the
alternative it already rejected, a partial rollup at the interim write,
is still the wrong call, and why round 16's NotEvaluatedReason machinery
doesn't change that: NotEvaluatedReason partitions per-criterion
inapplicability reasons; this is a document-level "did the post-loop
enrichment step run at all" flag, a different question with an already-
sound answer): an explicit `run_complete: bool` on `JSONResultBuilder`,
`False` by default (every interim write), set `True` only after the
post-loop enrichment, immediately before the final write. A file with no
`run_complete` key at all (any pre-round-17 file) must read as `False`
-- checked here via `bool(doc.get("run_complete"))`, never a bare
`.get("run_complete", True)`.

Also checked: what report_generator.py/summary.py (2 of GEPER's 3
renderers -- summary_short.py does not render model_checkpoints/
provenance at all, confirmed by grep, so there is no bare-identifier
regression possible there) do with a bare (unenriched) checkpoint
identifier. Before this round: render it with no caveat at all,
indistinguishable from a status the reader should trust. That's the
"second half" of the fix this file also covers.

Constraints followed: `pipeline.orchestrator` is NOT imported anywhere
in this file (confirmed ~218s / ~275MB via the TensorFlow/absl chain --
see test_mtdna_compartment_gate.py's own docstring for the same
constraint). The orchestrator wiring itself (`result_builder.run_complete
= True` landing strictly after `rollup_run_status`/
`finalize_model_checkpoint_provenance` and strictly before the final
`result_builder.write(json_path)`) is instead verified as a source-order
property directly on `pipeline/orchestrator.py`'s own text -- cheap,
exact, and it fails loudly if a future edit reorders these lines,
without ever importing the module.
"""

import re
import unittest
from pathlib import Path

from report import report_generator as report_generator_module
from report import summary as summary_module
from report.json_builder import JSONResultBuilder

_ORCHESTRATOR_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "orchestrator.py"

_ENRICHED_SPLICEBERT = {
    "identifier": "splicebert (HuggingFace BertForMaskedLM, see pipeline/models/splicebert_plugin.py)",
    "status": "failed",
    "reason": "timed out loading the checkpoint after 180s",
}
_BARE_SPLICEBERT = "splicebert (HuggingFace BertForMaskedLM, see pipeline/models/splicebert_plugin.py)"

_INCOMPLETE_NOTICE_FRAGMENT = "This run did not complete"


class TestJSONResultBuilderRunComplete(unittest.TestCase):
    def test_default_is_false(self):
        doc = JSONResultBuilder(input_vcf_path="x.vcf").build()
        self.assertIn("run_complete", doc)
        self.assertIs(doc["run_complete"], False)

    def test_set_true_is_reflected_in_build(self):
        builder = JSONResultBuilder(input_vcf_path="x.vcf")
        builder.run_complete = True
        self.assertIs(builder.build()["run_complete"], True)

    def test_write_before_marking_complete_persists_false(self):
        # Simulates the periodic in-loop checkpoint write: nothing sets
        # run_complete before this, so the document on disk at that
        # point must say False, not omit the key.
        import json
        import tempfile
        import os

        builder = JSONResultBuilder(input_vcf_path="x.vcf")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "geper_results.json")
            builder.write(path)
            with open(path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
        self.assertIs(on_disk["run_complete"], False)


class TestInputVcfAbsolutePath(unittest.TestCase):
    """Finding 0: `input_vcf` stores the raw --vcf argument as given, with
    no cwd recorded -- opening the document from a different working
    directory leaves no way to resolve a relative argument back to a real
    file. `input_vcf` itself is untouched (REPRODUCIBILITY_PROTOCOL.md
    treats it as a field that must match between two runs of the same
    input); `input_vcf_absolute_path` is additive."""

    def test_relative_argument_resolved_alongside_original(self):
        import os

        doc = JSONResultBuilder(input_vcf_path="./file.vcf").build()
        self.assertEqual(doc["input_vcf"], "./file.vcf")
        self.assertEqual(doc["input_vcf_absolute_path"], os.path.abspath("./file.vcf"))
        self.assertTrue(os.path.isabs(doc["input_vcf_absolute_path"]))


class TestMarkdownRendererShowsIncompleteRunNotice(unittest.TestCase):
    """`report/report_generator.py::ReportGenerator._render_provenance`.

    FIX #9 (2026-08-31): a `TestAbsentReadsAsIncomplete` class used to
    sit above this one, its docstring stating round 12's own
    requirement ('Absent must read as false everywhere it's checked --
    no reader may default a missing key to true') and its two tests
    (test_missing_key_is_falsy_via_bool_get, test_explicit_none_is_also_
    falsy) asserting `bool({}.get("run_complete"))` and
    `bool({"run_complete": None}.get("run_complete"))` are both `False`.
    Both are true of Python's `bool()`/`dict.get()` for ANY dict --
    neither test ever called report_generator.py, so neither could have
    caught a reader that used `.get("run_complete", True)`, the exact
    bug the requirement warns against. Removed rather than replaced:
    test_missing_run_complete_key_also_shows_notice below, and its
    twin in TestFullPdfRendererShowsIncompleteRunNotice, already drive
    the real renderer with a genuinely keyless document -- confirmed
    live by mutation: temporarily changing `_render_provenance`'s
    `bool(json_document.get("run_complete"))` to
    `bool(json_document.get("run_complete", True))` left the two
    now-removed tests green (they never touched this code) while these
    two went red; reverting restored green. The removed tests added no
    discriminating power the file didn't already have.
    """

    def test_incomplete_run_with_bare_identifiers_shows_notice(self):
        doc = {
            "code_version": "abc123",
            "model_checkpoints": {"splicebert": _BARE_SPLICEBERT},
            "run_complete": False,
            "provenance": [],
        }
        text = "\n".join(report_generator_module.ReportGenerator._render_provenance(doc))
        self.assertIn(_INCOMPLETE_NOTICE_FRAGMENT, text)

    def test_missing_run_complete_key_also_shows_notice(self):
        # No pre-round-17 file will ever have this key -- must not be
        # read as a complete run just because the key is absent.
        doc = {"code_version": "abc123", "model_checkpoints": {"splicebert": _BARE_SPLICEBERT}, "provenance": []}
        text = "\n".join(report_generator_module.ReportGenerator._render_provenance(doc))
        self.assertIn(_INCOMPLETE_NOTICE_FRAGMENT, text)

    def test_complete_run_with_enriched_status_shows_no_notice(self):
        doc = {
            "code_version": "abc123",
            "model_checkpoints": {"splicebert": _ENRICHED_SPLICEBERT},
            "run_complete": True,
            "provenance": [],
        }
        text = "\n".join(report_generator_module.ReportGenerator._render_provenance(doc))
        self.assertNotIn(_INCOMPLETE_NOTICE_FRAGMENT, text)
        # The real per-model status must still render as before.
        self.assertIn("failed", text.lower())
        self.assertIn("timed out loading the checkpoint after 180s", text)


class TestFullPdfRendererShowsIncompleteRunNotice(unittest.TestCase):
    """`report/summary.py::_build_provenance_flowables` -- the full PDF's
    own copy of the same section (round-review F1a noted the PDF had no
    reproducibility record at all until it was wired to read the same
    document fields as the Markdown path; this round's fix must cover
    both the same way that one did)."""

    def _rendered_text(self, document):
        styles = summary_module._build_stylesheet()
        flow = summary_module._build_provenance_flowables(document, styles)
        return "\n".join(getattr(f, "text", "") for f in flow if hasattr(f, "text"))

    def test_incomplete_run_shows_notice(self):
        doc = {
            "code_version": "abc123",
            "model_checkpoints": {"splicebert": _BARE_SPLICEBERT},
            "run_complete": False,
            "provenance": [],
        }
        self.assertIn(_INCOMPLETE_NOTICE_FRAGMENT, self._rendered_text(doc))

    def test_missing_run_complete_key_also_shows_notice(self):
        doc = {"code_version": "abc123", "model_checkpoints": {"splicebert": _BARE_SPLICEBERT}, "provenance": []}
        self.assertIn(_INCOMPLETE_NOTICE_FRAGMENT, self._rendered_text(doc))

    def test_complete_run_shows_no_notice(self):
        doc = {
            "code_version": "abc123",
            "model_checkpoints": {"splicebert": _ENRICHED_SPLICEBERT},
            "run_complete": True,
            "provenance": [],
        }
        self.assertNotIn(_INCOMPLETE_NOTICE_FRAGMENT, self._rendered_text(doc))


class TestShortPdfDoesNotRenderCheckpointsAtAll(unittest.TestCase):
    """Confirms the scope claim in this file's module docstring: only 2
    of GEPER's 3 renderers touch `model_checkpoints`/`provenance` at
    all, so only those 2 needed this round's fix."""

    def test_summary_short_module_has_no_checkpoint_or_provenance_code(self):
        source = Path(summary_module.__file__).resolve()
        source = source.parent / "summary_short.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("model_checkpoints", text)
        self.assertNotIn("provenance", text.lower())


class TestOrchestratorSourceOrdering(unittest.TestCase):
    """Verifies the reported ordering directly against
    `pipeline/orchestrator.py`'s own source text, without importing the
    module (see this file's module docstring for the ~218s/275MB cost
    that import carries on this machine). Fails loudly if a future edit
    reorders these lines relative to each other."""

    source: str = ""

    @classmethod
    def setUpClass(cls):
        cls.source = _ORCHESTRATOR_PATH.read_text(encoding="utf-8")

    def _first_index(self, pattern: str) -> int:
        match = re.search(pattern, self.source)
        assert match is not None, f"pattern not found in orchestrator.py: {pattern!r}"
        return match.start()

    def test_in_loop_checkpoint_write_precedes_enrichment(self):
        in_loop_write = self._first_index(r"CONFIG\.CHECKPOINT_INTERVAL")
        enrichment = self._first_index(r"rollup_run_status\(")
        self.assertLess(in_loop_write, enrichment)

    def test_enrichment_precedes_marking_run_complete(self):
        enrichment = self._first_index(r"finalize_model_checkpoint_provenance\(")
        marked_complete = self._first_index(r"result_builder\.run_complete\s*=\s*True")
        self.assertLess(enrichment, marked_complete)

    def test_marking_run_complete_precedes_final_write(self):
        marked_complete = self._first_index(r"result_builder\.run_complete\s*=\s*True")
        # The final `result_builder.write(json_path)` call, distinct
        # from the in-loop one -- searched for after `marked_complete`
        # so this doesn't accidentally match the earlier in-loop call.
        final_write_match = re.search(r"result_builder\.write\(json_path\)", self.source[marked_complete:])
        self.assertIsNotNone(
            final_write_match, "final result_builder.write(json_path) call not found after marking complete"
        )

    def test_run_complete_is_never_set_true_inside_the_variant_loop(self):
        # The in-loop write happens inside a `for i, variant in
        # enumerate(variants, ...)` block; `run_complete = True` must
        # appear only after that loop's own body, i.e. after the
        # post-loop enrichment call, not interleaved with it.
        loop_start = self._first_index(r"for i, variant in enumerate\(variants")
        enrichment = self._first_index(r"rollup_run_status\(")
        self.assertLess(loop_start, enrichment)


if __name__ == "__main__":
    unittest.main()
