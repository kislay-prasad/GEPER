"""
"Which version produced this result?" -- the model half.

`pipeline/provenance.py::get_model_checkpoint_identifiers` records one
string per model. Five of those strings were POINTERS TO A SOURCE FILE
rather than versions ("spliceformer (see pipeline/models/
spliceformer_plugin.py for checkpoint URL)"), which is a known and
honest gap: a reader can see the field is not a version.

One was worse than a gap. `mmsplice` recorded

    "mmsplice==2.4.0 (pinned, see requirements.txt)"

while the only code path that installs the package
(`pipeline/models/mmsplice/loader.py::_ensure_mmsplice_package_files_available`)
shells out to a BARE, UNPINNED `pip install --no-deps mmsplice` -- and
`requirements.txt` carries no mmsplice requirement line at all and
explicitly instructs the reader NOT to add one. So the string asserted
a version nothing enforced, and cited as its authority a file that
both lacks the pin and forbids it. A gap tells a reader "look
elsewhere"; this told them "2.4.0" and named a source that would
appear to confirm it.

These tests fix both halves of that -- the value and the citation --
and they are deliberately environment-independent: `mmsplice` is not
installed in this repo's test environment, so nothing here may be
gated on its presence. `test_recorded_version_follows_the_installed
_one` injects a resolver rather than requiring a real install, for
the same reason `26a14b0` restored seven mocks: a skipped test is not
a passing one.

Also covered:
  * Enformer/Borzoi passing an explicit `revision=` to
    `from_pretrained`, so two runs a month apart cannot load different
    weights while every recorded field stays byte-identical (the
    record asserting sameness, rather than omitting a difference).
  * A resumed run keeping the PRIOR run's checkpoint record instead of
    stamping carried-forward variants with this run's, in the three
    states a resumed run actually has -- ran-now, carried-forward, and
    prior-value-unknown for a document written before any of this
    existed. The third is not the second: it is the state every
    document already on disk is in.
"""

import os
import re
import unittest
from unittest import mock

from pipeline.provenance import (
    finalize_model_checkpoint_provenance,
    get_model_checkpoint_identifiers,
)
from report.json_builder import JSONResultBuilder

_REQUIREMENTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt")


def _mmsplice_entry(identifiers):
    return identifiers["mmsplice"]


class MMSpliceVersionClaimTests(unittest.TestCase):
    """The false-claim half. Every assertion here is red against the pre-fix tree."""

    def test_requirements_txt_does_not_pin_mmsplice(self):
        """
        The premise the other tests rest on, asserted rather than
        assumed: the cited authority carries no such pin. If someone
        later adds a real `mmsplice==X` requirement line, this test
        fails and tells them to revisit the provenance string too --
        which is the coupling the old comment only implied.
        """
        with open(_REQUIREMENTS_PATH, "r", encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
        requirement_lines = [ln for ln in lines if ln and not ln.startswith("#")]
        mmsplice_requirements = [ln for ln in requirement_lines if re.match(r"^mmsplice\b", ln, re.IGNORECASE)]
        self.assertEqual(
            mmsplice_requirements,
            [],
            "requirements.txt now carries an mmsplice requirement line; "
            "pipeline/provenance.py's mmsplice identifier must be revisited.",
        )

    def test_identifier_does_not_cite_requirements_txt_as_pin_authority(self):
        """
        The citation half, which the human called the more serious of
        the two: a true value with a false provenance is still false
        provenance. Nothing may point at requirements.txt for a pin
        that file forbids.
        """
        entry = _mmsplice_entry(get_model_checkpoint_identifiers())
        self.assertNotIn("requirements.txt", entry)

    def test_identifier_does_not_hardcode_a_version_literal(self):
        """
        A literal in the provenance string cannot track what pip
        actually installed, so it is wrong the moment upstream moves.
        """
        entry = _mmsplice_entry(get_model_checkpoint_identifiers())
        self.assertNotRegex(
            entry,
            r"\d+\.\d+\.\d+",
            "the startup identifier must describe the install mechanism, not assert a version it cannot know",
        )

    def test_startup_identifier_names_the_install_as_unpinned(self):
        """
        Honest is not the same as empty. Unpinned-by-design is a real
        fact about this dependency and belongs in the record.
        """
        entry = _mmsplice_entry(get_model_checkpoint_identifiers()).lower()
        self.assertIn("unpinned", entry)


class ResolvedVersionTests(unittest.TestCase):
    """
    `finalize_model_checkpoint_provenance` runs after every variant, so
    it is the only point at which "which version actually loaded" is
    knowable. Resolution is injected here rather than requiring a real
    mmsplice install -- see this module's docstring.
    """

    @staticmethod
    def _used(model_key):
        return {model_key: {"status": "used", "reason": "ran"}}

    def test_recorded_version_follows_the_installed_one(self):
        with mock.patch(
            "pipeline.provenance.installed_package_version",
            return_value="2.9.9",
        ):
            enriched = finalize_model_checkpoint_provenance(get_model_checkpoint_identifiers(), self._used("mmsplice"))
        self.assertEqual(enriched["mmsplice"]["resolved_version"], "2.9.9")

    def test_a_different_install_produces_a_different_record(self):
        """
        The whole point of resolving: the field must MOVE when the
        install moves. A constant that happens to match today would
        pass the test above and fail this one.
        """
        versions = []
        for injected in ("2.4.0", "3.0.1"):
            with mock.patch("pipeline.provenance.installed_package_version", return_value=injected):
                enriched = finalize_model_checkpoint_provenance(
                    get_model_checkpoint_identifiers(), self._used("mmsplice")
                )
            versions.append(enriched["mmsplice"]["resolved_version"])
        self.assertEqual(versions, ["2.4.0", "3.0.1"])

    def test_absent_install_records_no_version_rather_than_a_guess(self):
        """
        `mmsplice` is genuinely not installed in this environment. The
        honest record is the absence of a resolved version, never a
        fallback to the configured/expected one.
        """
        with mock.patch("pipeline.provenance.installed_package_version", return_value=None):
            enriched = finalize_model_checkpoint_provenance(get_model_checkpoint_identifiers(), self._used("mmsplice"))
        self.assertIsNone(enriched["mmsplice"]["resolved_version"])

    def test_a_model_that_did_not_run_reports_no_resolved_version(self):
        """
        The record answers "which version produced this result". A
        model that produced no result has no version to report, and
        reporting the configured pin would assert a load that never
        happened.
        """
        enriched = finalize_model_checkpoint_provenance(
            get_model_checkpoint_identifiers(),
            {"mmsplice": {"status": "disabled", "reason": "not enabled in this configuration"}},
        )
        self.assertIsNone(enriched["mmsplice"]["resolved_version"])


class PinnedIdentifierTests(unittest.TestCase):
    """
    Part 4: the values already exist in the source; they were simply
    never carried into the document.
    """

    def test_already_pinned_models_carry_their_pin(self):
        identifiers = get_model_checkpoint_identifiers()
        for key, module_path, symbol in (
            ("esm2", "models.esm2", "_ESM2_REVISION"),
            ("splicebert", "pipeline.models.splicebert.loader", "DEFAULT_ZENODO_RECORD"),
            ("spliceformer", "pipeline.models.spliceformer.loader", "DEFAULT_SOURCE_REF"),
        ):
            with self.subTest(model=key):
                module = __import__(module_path, fromlist=[symbol])
                pin = getattr(module, symbol)
                self.assertIn(
                    str(pin),
                    identifiers[key],
                    f"{key}'s identifier does not carry {module_path}.{symbol}",
                )

    def test_hyenadna_carries_the_checkpoint_revision_for_the_configured_model(self):
        from config import CONFIG
        from models.hyenadna import _HYENADNA_CHECKPOINT_REVISIONS

        expected = _HYENADNA_CHECKPOINT_REVISIONS.get(CONFIG.models.HYENADNA_MODEL_NAME)
        if expected is None:
            self.skipTest("no revision pinned for the configured HyenaDNA model")
        self.assertIn(expected, get_model_checkpoint_identifiers()["hyenadna_model_name"])

    def test_no_identifier_is_a_bare_source_file_pointer(self):
        """
        The shape this whole change exists to remove: a provenance
        string whose only content is "go read this .py file".
        `alphamissense_catalogue_source` is exempt -- it names a data
        catalogue, not a model, and points at the data-source
        provenance list which has its own version machinery.
        """
        for key, entry in get_model_checkpoint_identifiers().items():
            if key == "alphamissense_catalogue_source":
                continue
            with self.subTest(model=key):
                self.assertNotRegex(
                    entry,
                    r"see\s+pipeline/models/\S+\.py",
                    f"{key} still records a source-file pointer instead of a version identifier",
                )


class HuggingFaceRevisionPinTests(unittest.TestCase):
    """
    Part 2. Without `revision=`, `from_pretrained` resolves whatever
    the repo's default branch holds that day: two runs a month apart
    can load different weights while every recorded field stays
    byte-identical. The record does not omit the difference -- it
    asserts sameness.
    """

    def test_enformer_passes_its_pinned_revision(self):
        # `enformer_pytorch` is imported inside `_load_impl`, so the
        # patch target is the module in sys.modules, not an attribute
        # of the plugin module.
        from pipeline.models import enformer_plugin

        fake = mock.MagicMock()
        fake.from_pretrained.return_value = mock.MagicMock()
        # Auto-install is disabled under pytest, so the plugin's own
        # availability guard raises before the load is ever attempted.
        # Reporting the package PRESENT is what puts `_load_impl` on the
        # path under test; it installs nothing.
        with (
            mock.patch.dict("sys.modules", {"enformer_pytorch": fake}),
            mock.patch.object(
                enformer_plugin,
                "check_pip_package_availability",
                return_value=enformer_plugin.PackageCheckStatus.PRESENT,
            ),
        ):
            try:
                enformer_plugin.EnformerPlugin()._load_impl()
            except Exception:  # noqa: BLE001 -- only the call kwargs are under test
                pass
        self.assertTrue(fake.from_pretrained.called, "from_pretrained was never reached")
        self.assertEqual(
            fake.from_pretrained.call_args.kwargs.get("revision"),
            enformer_plugin._ENFORMER_REVISION,
        )

    def test_borzoi_passes_its_pinned_revision(self):
        from pipeline.models import borzoi_plugin

        fake = mock.MagicMock()
        fake.Borzoi.from_pretrained.return_value = mock.MagicMock()
        with (
            mock.patch.dict("sys.modules", {"borzoi_pytorch": fake}),
            mock.patch.object(
                borzoi_plugin,
                "check_pip_package_availability",
                return_value=borzoi_plugin.PackageCheckStatus.PRESENT,
            ),
        ):
            try:
                borzoi_plugin.BorzoiPlugin()._load_impl()
            except Exception:  # noqa: BLE001 -- only the call kwargs are under test
                pass
        self.assertTrue(fake.Borzoi.from_pretrained.called, "from_pretrained was never reached")
        self.assertEqual(
            fake.Borzoi.from_pretrained.call_args.kwargs.get("revision"),
            borzoi_plugin._BORZOI_REVISION,
        )

    def test_pins_are_full_commit_shas(self):
        from pipeline.models import borzoi_plugin, enformer_plugin

        for name, pin in (
            ("enformer", enformer_plugin._ENFORMER_REVISION),
            ("borzoi", borzoi_plugin._BORZOI_REVISION),
        ):
            with self.subTest(model=name):
                self.assertRegex(pin, r"^[0-9a-f]{40}$", "a branch name or tag is not a revision pin")


class ResumedRunProvenanceTests(unittest.TestCase):
    """
    Part 3, and the constraint that decides whether it is right: a
    resumed run has THREE states, not two. Carrying forward a variant
    must not stamp it with this run's checkpoint record, and a prior
    document that predates version recording is not the same as one
    that recorded nothing.
    """

    def _builder(self):
        return JSONResultBuilder(
            input_vcf_path="in.vcf",
            assembly="GRCh38",
            model_checkpoints={"esm2": "this-run"},
        )

    def test_a_fresh_run_carries_no_prior_provenance(self):
        document = self._builder().build()
        self.assertEqual(document["carried_forward_provenance"], [])

    def test_prior_checkpoints_are_kept_not_overwritten(self):
        builder = self._builder()
        builder.note_carried_forward_provenance(
            {
                "generated_at": "2026-08-01T00:00:00+00:00",
                "code_version": "abc123",
                "model_checkpoints": {"esm2": "prior-run"},
            },
            variant_count=3,
        )
        document = builder.build()

        self.assertEqual(document["model_checkpoints"], {"esm2": "this-run"})
        carried = document["carried_forward_provenance"]
        self.assertEqual(len(carried), 1)
        self.assertEqual(carried[0]["state"], "carried_forward")
        self.assertEqual(carried[0]["model_checkpoints"], {"esm2": "prior-run"})
        self.assertEqual(carried[0]["variant_count"], 3)
        self.assertEqual(carried[0]["code_version"], "abc123")

    def test_a_prior_document_predating_this_field_is_its_own_state(self):
        """
        The state god warned not to collapse, and the one every
        document already on disk is in. It must not render as
        "carried forward, checkpoints {}" -- that would claim the
        earlier run recorded nothing, when in truth we never asked.
        """
        builder = self._builder()
        builder.note_carried_forward_provenance(
            {"generated_at": "2026-07-01T00:00:00+00:00"},
            variant_count=2,
        )
        carried = builder.build()["carried_forward_provenance"]

        self.assertEqual(carried[0]["state"], "prior_value_unknown")
        self.assertIsNone(carried[0]["model_checkpoints"])
        self.assertTrue(carried[0]["reason"], "the unknown state must say why")

    def test_the_two_prior_states_are_distinguishable(self):
        """
        A reader must be able to tell "the earlier run used these
        models" from "we cannot know what the earlier run used".
        """
        builder = self._builder()
        builder.note_carried_forward_provenance({"model_checkpoints": {"esm2": "prior"}}, variant_count=1)
        builder.note_carried_forward_provenance({}, variant_count=1)
        states = [entry["state"] for entry in builder.build()["carried_forward_provenance"]]
        self.assertEqual(states, ["carried_forward", "prior_value_unknown"])

    def test_an_empty_recorded_checkpoint_map_is_not_unknown(self):
        """
        The inverse of the test above: a prior run that genuinely
        recorded an empty map DID answer the question, and must not be
        demoted to unknown.
        """
        builder = self._builder()
        builder.note_carried_forward_provenance({"model_checkpoints": {}}, variant_count=1)
        entry = builder.build()["carried_forward_provenance"][0]
        self.assertEqual(entry["state"], "carried_forward")
        self.assertEqual(entry["model_checkpoints"], {})


class ResumeWiringTests(unittest.TestCase):
    """
    The builder contract above is only half the fix: the orchestrator
    has to actually hand it the prior document. This drives
    `GeperPipeline`'s own resume hook, which is a static method
    precisely so it can be exercised without constructing an
    orchestrator (the suite has no harness for one).
    """

    def test_the_resume_hook_hands_over_the_prior_document_and_count(self):
        from pipeline.orchestrator import GeperPipeline

        builder = JSONResultBuilder(input_vcf_path="in.vcf", model_checkpoints={"esm2": "this-run"})
        prior = {"generated_at": "2026-08-01T00:00:00+00:00", "model_checkpoints": {"esm2": "prior-run"}}

        GeperPipeline._carry_forward_prior_provenance(builder, prior, {"1:100:A>T", "1:200:C>G", "1:300:G>A"})

        entry = builder.build()["carried_forward_provenance"][0]
        self.assertEqual(entry["state"], "carried_forward")
        self.assertEqual(entry["model_checkpoints"], {"esm2": "prior-run"})
        # DISTINCT variants carried, matching the count the resume log
        # line reports -- a wrong number here would misdescribe how much
        # of the document the prior run is answerable for.
        self.assertEqual(entry["variant_count"], 3)

    def test_a_prior_document_without_checkpoints_stays_unknown_through_the_hook(self):
        """
        The third state has to survive the wiring, not just the builder:
        this is the state every document written before this change is
        in, so it is the one a real resume will hit first.
        """
        from pipeline.orchestrator import GeperPipeline

        builder = JSONResultBuilder(input_vcf_path="in.vcf", model_checkpoints={"esm2": "this-run"})
        GeperPipeline._carry_forward_prior_provenance(builder, {"variants": []}, {"1:100:A>T"})
        self.assertEqual(builder.build()["carried_forward_provenance"][0]["state"], "prior_value_unknown")


class ProvenanceSelfCitationTests(unittest.TestCase):
    """
    Constraint C: a string that names its own authority is making a
    claim, and that claim must be true. Three stale ones surfaced this
    week, so this sweeps the whole identifier set rather than the one
    we happened to find.
    """

    def test_every_cited_path_exists(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cited = re.compile(r"(?:see\s+)((?:[\w.\-]+/)+[\w.\-]+\.(?:py|txt|md))")
        for key, entry in get_model_checkpoint_identifiers().items():
            for path in cited.findall(str(entry)):
                with self.subTest(model=key, path=path):
                    self.assertTrue(
                        os.path.exists(os.path.join(repo_root, path)),
                        f"{key} cites '{path}', which does not exist",
                    )


if __name__ == "__main__":
    unittest.main()
