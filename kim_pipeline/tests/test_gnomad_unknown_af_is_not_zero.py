"""
Card: unknown-gnomad-af-fabricated-as-a-measured-zero.

`pipeline/gnomad/lookup.py` builds a `GnomadHit` on three paths (local
tabix, GraphQL API, disk cache). On all three, an allele frequency that
the source did not supply was coerced to `0.0` with `x or 0.0`:

    :187-188  af=entry["af"] or 0.0, af_popmax=entry["af_popmax"] or 0.0
    :410      if af_popmax is None: af_popmax = af or 0.0
    :413      return GnomadHit(af=af or 0.0, ...)
    :533      af = float(genome.get("af") or 0.0)
    :536      af_popmax = 0.0        # never raised when `populations` is empty

`0.0` is not a neutral placeholder here -- it is the single most
PM2-favourable value the field can hold. Traced through:

    GnomadHit(af=0.0)
      -> orchestration/shared.py: gnomad_af = 0.0, gnomad_af_absent = False
      -> acmg/classifier.py::_pm2 -- `e.gnomad_af is None and
         e.gnomad_af_popmax is None` is FALSE, so the "Unknown /
         Insufficient Data" branch is skipped entirely
      -> af = 0.0 ; met = af < pm2_af_max -> PM2 AWARDED
         reason text: "AF=0.00e+00 < PM2 threshold ..."

So a report asserts a measured population frequency of zero for a
variant nobody measured, and feeds that assertion to a
pathogenic-supporting criterion.

The distinction being destroyed is the one this module is otherwise
careful about: `GnomadLookupOutcome`'s own docstring calls ABSENT /
PRESENT / UNAVAILABLE "the three states that the caller needs to
differentiate", and spells out that PM2 *should* be awarded for ABSENT
and must NOT be for UNAVAILABLE. A PRESENT hit whose frequency is
unknown is a fourth state the enum does not model, and `or 0.0`
resolved it to the most favourable answer available.

THE FIX IS PRESENCE, NOT TRUTHINESS. `Optional[float]` already carries
the fourth state: `None` means "the source did not give us a number",
`0.0` means "the source gave us zero". No new enum member is needed --
and adding one would be worse, because `GnomadLookupOutcome` is one of
the 7 enums in this repo with no `__bool__` guard, so any future
`if outcome:` would silently take the truthy branch for every member.
The type change is what makes the two states distinguishable at all;
`or 0.0` is only the mechanism that erased them.

The consumers were already written for `None` and only the producer
fabricated a value -- `_pm2`'s guard (classifier.py:581),
`reporting/stage.py`'s `if gnomad_af is not None:` and
`reporting/pdf_report.py`'s `isinstance(af, (int, float))` all handle
it, the middle one with a comment calling the case "unexpected". That
is the shape of this defect: the honest state had somewhere to go the
whole time.

WHAT THIS FILE ASSERTS, and why it is split three ways:

  A. THE PRODUCER (the actual fix, and the only part that goes RED).
     One test per reachable trigger the audit named: AF= absent, AF=.
     unparseable, AF_popmax absent, API `af` null, API `populations`
     empty, and a cache row with a NULL af.

  B. THE CONSEQUENCE, end to end, through the REAL producer inside the
     REAL `run_acmg_evidence_batch` -- only tabix's subprocess is
     mocked, not `GnomadLookup.lookup`. Both halves the human required:
     PM2 must not fire, AND the rendered report must not state a
     frequency. PM2 not firing is not sufficient on its own.

  C. CONTROLS. A GENUINELY measured 0.0 must survive as 0.0 and still
     award PM2 on its own merits. This is the test that distinguishes a
     presence fix from a truthiness fix: a fix that made "unknown" work
     by treating every zero as unknown would pass A and B and fail here.
     Confirmed-absent and a common variant are pinned unchanged too.

Note on class B, in the spirit of this suite's own recorded lesson
(`test_gnomad_lookup_failure_not_absent.py`, the CORRECTION note): a
test that passes identically before and after a fix has not been shown
to test that fix. Class B drives the real producer precisely so that it
DOES go red pre-fix. Hand-constructing a `GnomadHit(af=None, ...)` and
feeding it to the classifier would have been green both times, because
the classifier was never the broken part.
"""

import os
import unittest
from unittest import mock

from pipeline.acmg.classifier import AcmgClassifier  # noqa: F401  (imported for the real batch path)
from pipeline.gnomad.cache import GnomadDiskCache
from pipeline.gnomad.lookup import GnomadHit, GnomadLookup, GnomadLookupOutcome
from pipeline.orchestration.shared import run_acmg_evidence_batch
from pipeline.reporting.stage import _acmg_to_html_table


CHROM, POS, REF, ALT = "17", 43057051, "A", "T"


def _vcf_line(info: str) -> str:
    """One tabix output record matching CHROM/POS/REF/ALT, with `info` as
    the INFO column. `_tabix_lookup` requires >= 8 tab-separated fields."""
    return "\t".join([CHROM, str(POS), ".", REF, ALT, ".", "PASS", info])


def _api_payload(genome):
    return {"data": {"variant": {"genome": genome}}}


class _Completed:
    """Stand-in for the object `_run` returns; only `.stdout` is read."""

    def __init__(self, stdout: str):
        self.stdout = stdout


def _local_cfg(tmpdir, vcf_name="gnomad.grch38.vcf.gz"):
    """A config whose backend resolves to local tabix. `os.path.isfile`
    must succeed for the backend to be chosen, so the file is real (and
    empty -- nothing ever reads it, `_run` is mocked)."""
    vcf = os.path.join(tmpdir, vcf_name)
    with open(vcf, "w", encoding="utf-8"):
        pass
    return {
        "gnomad": {"enabled": True, "vcf_path": vcf, "cache_dir": tmpdir},
        "clinvar": {"enabled": False},
        "vep": {"enabled": False},
    }


def _api_cfg(tmpdir):
    return {
        "gnomad": {"enabled": True, "vcf_path": "/nonexistent/path.vcf.gz", "cache_dir": tmpdir},
        "clinvar": {"enabled": False},
        "vep": {"enabled": False},
    }


class _TabixMocked:
    """Context manager patching the two subprocess seams `_tabix_lookup`
    goes through, so the real parsing/GnomadHit-building code runs."""

    def __init__(self, stdout: str):
        self._stdout = stdout
        self._patches: list = []

    def __enter__(self):
        self._patches = [
            mock.patch("pipeline.gnomad.lookup._require", return_value="tabix"),
            mock.patch("pipeline.gnomad.lookup._run", return_value=_Completed(self._stdout)),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


# ---------------------------------------------------------------------------
# A. THE PRODUCER
# ---------------------------------------------------------------------------


class TestUnknownFrequencyStaysUnknownAtTheProducer(unittest.TestCase):
    def setUp(self):
        # `mkdtemp` + `ignore_errors`, not `TemporaryDirectory`: the gnomAD
        # disk cache holds an open sqlite3 connection for the life of the
        # GnomadLookup, and Windows refuses to unlink an open file, so a
        # strict cleanup turns every test in the class into a teardown
        # error that hides its real result.
        import shutil
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    # ── local tabix backend ────────────────────────────────────────────────

    def test_tabix_record_with_no_af_field_reports_unknown_not_zero(self):
        """THE DANGEROUS CASE, shape 1: the record matched -- the variant IS
        in the file -- but its INFO carries no AF=."""
        gn = GnomadLookup(cfg=_local_cfg(self.tmpdir))
        with _TabixMocked(_vcf_line("AC=5;AN=1000")):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIsInstance(hit, GnomadHit, "a matched record is still PRESENT")
        self.assertIsNone(
            hit.af,
            "an AF the source never supplied must arrive as None (unknown), "
            "not as 0.0 -- 0.0 is a measurement claim nobody made",
        )
        self.assertNotEqual(hit.af, 0.0)

    def test_tabix_record_with_unparseable_af_reports_unknown_not_zero(self):
        """Shape 2: `AF=.` -- the VCF spec's own 'missing value'. It reaches
        `_parse_info_float`'s `except ValueError` and returns None."""
        gn = GnomadLookup(cfg=_local_cfg(self.tmpdir))
        with _TabixMocked(_vcf_line("AF=.;AC=5;AN=1000")):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIsInstance(hit, GnomadHit)
        self.assertIsNone(hit.af, "an unparseable AF is unknown, not zero")

    def test_tabix_missing_af_popmax_is_not_fabricated_from_af(self):
        """Shape 3: AF is present and real, AF_popmax is absent. popmax must
        stay unknown rather than being back-filled -- `_pm2` PREFERS popmax
        over af (classifier.py:595), so a fabricated popmax outranks a real
        measurement."""
        gn = GnomadLookup(cfg=_local_cfg(self.tmpdir))
        with _TabixMocked(_vcf_line("AF=0.02;AC=20;AN=1000")):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIsInstance(hit, GnomadHit)
        self.assertEqual(hit.af, 0.02, "the AF that WAS supplied must survive untouched")
        self.assertIsNone(
            hit.af_popmax,
            "a popmax nobody reported must not be invented -- and inventing 0.0 "
            "here would override the real AF=0.02 at classifier.py:595",
        )

    # ── GraphQL API backend ────────────────────────────────────────────────

    def test_api_genome_with_null_af_reports_unknown_not_zero(self):
        """Shape 4: the API answered, the variant IS in gnomAD (`genome` is
        not null), but `af` is null -- the field is nullable in the schema
        this module queries."""
        gn = GnomadLookup(cfg=_api_cfg(self.tmpdir))
        payload = _api_payload({"af": None, "populations": [{"id": "afr", "ac": 3, "an": 2000}]})
        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        resp.json.return_value = payload
        with mock.patch("pipeline.gnomad.lookup.requests.post", return_value=resp):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIsInstance(hit, GnomadHit, "genome present means the variant is PRESENT")
        self.assertIsNone(hit.af, "a null af from the API is unknown, not zero")

    def test_api_response_without_populations_does_not_fabricate_a_popmax_of_zero(self):
        """Shape 5 -- the site the original finding MISSED, found while
        enumerating before editing. `af_popmax` is seeded to 0.0 and only
        ever raised inside the `for pop in populations` loop, so an empty or
        absent `populations` array leaves a fabricated popmax of 0.0 behind.

        This is strictly worse than the af case: `_pm2` prefers popmax over
        af, so a real, common AF of 0.02 is OVERRIDDEN by the invented zero
        and PM2 fires on a variant the API reported as common."""
        gn = GnomadLookup(cfg=_api_cfg(self.tmpdir))
        payload = _api_payload({"af": 0.02, "populations": []})
        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        resp.json.return_value = payload
        with mock.patch("pipeline.gnomad.lookup.requests.post", return_value=resp):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIsInstance(hit, GnomadHit)
        self.assertEqual(hit.af, 0.02, "the AF the API DID report must survive")
        self.assertIsNone(
            hit.af_popmax,
            "no per-population data means popmax is unknown; 0.0 here would "
            "outrank the real af=0.02 at classifier.py:595 and award PM2 to a "
            "variant gnomAD called common",
        )

    # ── disk cache backend ─────────────────────────────────────────────────

    def test_disk_cache_row_with_null_af_reports_unknown_not_zero(self):
        """Shape 6: the cache columns are nullable SQLite REALs and
        `cache.put` already accepts `Optional[float]`, so a NULL af is
        representable and round-trips -- until `_disk_entry_to_result`
        collapses it with `or 0.0` on the way back out."""
        cache = GnomadDiskCache(path=os.path.join(self.tmpdir, "gnomad_cache.sqlite3"))
        key = f"{CHROM}:{POS}:{REF}:{ALT}"
        cache.put(key, "present", af=None, af_popmax=None, ac=5, an=1000, backend="api")

        gn = GnomadLookup(cfg=_api_cfg(self.tmpdir))
        hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIsInstance(hit, GnomadHit, "a cached PRESENT row is still PRESENT")
        self.assertIsNone(hit.af, "a NULL af in the cache is unknown, not zero")
        self.assertIsNone(hit.af_popmax, "a NULL af_popmax in the cache is unknown, not zero")


# ---------------------------------------------------------------------------
# B. THE CONSEQUENCE -- both halves the human required
# ---------------------------------------------------------------------------


class TestPM2DoesNotFireOnAFrequencyNobodyMeasured(unittest.TestCase):
    """Drives the REAL `GnomadLookup` inside the REAL
    `run_acmg_evidence_batch`; only tabix's subprocess seam is mocked. If
    `GnomadLookup.lookup` itself were mocked here, the producer -- the part
    that is actually broken -- would never run, and these tests would pass
    before and after the fix alike."""

    def setUp(self):
        # `mkdtemp` + `ignore_errors`, not `TemporaryDirectory`: the gnomAD
        # disk cache holds an open sqlite3 connection for the life of the
        # GnomadLookup, and Windows refuses to unlink an open file, so a
        # strict cleanup turns every test in the class into a teardown
        # error that hides its real result.
        import shutil
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _run_batch(self, info: str):
        variant = {
            "chrom": CHROM,
            "pos": POS,
            "ref": REF,
            "alt": ALT,
            "gene_name": "BRCA1",
            "consequence": "missense_variant",
        }
        with _TabixMocked(_vcf_line(info)):
            with mock.patch("pipeline.gnomad.lookup.GnomadLookup.lookup_batch", return_value=None):
                results = run_acmg_evidence_batch(
                    [variant], _local_cfg(self.tmpdir), sample_id="TESTSAMPLE"
                )
        return results[0]

    def test_a_matched_record_without_an_af_does_not_award_pm2(self):
        """Half one, in the human's words: 'matched record, AF absent or
        unparseable, PM2 must not fire'."""
        r = self._run_batch("AC=5;AN=1000")
        self.assertNotIn(
            "PM2",
            r["criteria_met"],
            "PM2 was awarded on a frequency nobody measured; got %r" % (r,),
        )
        self.assertIn(
            "PM2",
            r["criteria_unknown"],
            "an unmeasured frequency is Unknown / Insufficient Data, not a negative finding",
        )

    def test_the_report_does_not_state_a_frequency_that_was_never_measured(self):
        """Half two, and NOT implied by half one: 'the report must not state
        a frequency'. A fix that silenced PM2 while still printing
        'AF: 0.00e+00' in the variant table would satisfy the first
        assertion and still publish the false measurement."""
        r = self._run_batch("AC=5;AN=1000")
        html = _acmg_to_html_table([r])
        self.assertNotIn(
            "0.00e+00",
            html,
            "the report stated a measured allele frequency of zero for a "
            "variant whose frequency was never obtained",
        )
        self.assertNotIn(
            "AF:",
            html,
            "no numeric frequency of any value may be rendered for this variant",
        )
        # The cell must say something TRUE, not merely omit the number. Two
        # separate claims, because a fix could satisfy either alone:
        self.assertIn(
            "Found in gnomAD",
            html,
            "the lookup succeeded and the variant WAS found -- the report must still say so",
        )
        self.assertIn(
            "no allele frequency reported",
            html,
            "and it must say which part is missing",
        )
        # THE REGRESSION THIS PINS. Fixing the producer alone made the report
        # blame a network/tabix error for a lookup that succeeded -- swapping a
        # wrong number for a wrong diagnosis, and sending a reader to debug a
        # network that is fine. `_availability_fields` compares the tri-state
        # explicitly now; this is the assertion that keeps it that way.
        self.assertNotIn(
            "network/tabix error",
            html,
            "nothing failed: the lookup succeeded, found the variant, and "
            "simply carried no frequency",
        )

    def test_an_unparseable_af_takes_the_same_two_guarantees(self):
        """The `AF=.` trigger, through the same end-to-end path."""
        r = self._run_batch("AF=.;AC=5;AN=1000")
        self.assertNotIn("PM2", r["criteria_met"])
        self.assertNotIn("0.00e+00", _acmg_to_html_table([r]))


# ---------------------------------------------------------------------------
# C. CONTROLS -- what the fix must NOT change
# ---------------------------------------------------------------------------


class TestGenuinelyMeasuredValuesAreUnaffected(unittest.TestCase):
    def setUp(self):
        # `mkdtemp` + `ignore_errors`, not `TemporaryDirectory`: the gnomAD
        # disk cache holds an open sqlite3 connection for the life of the
        # GnomadLookup, and Windows refuses to unlink an open file, so a
        # strict cleanup turns every test in the class into a teardown
        # error that hides its real result.
        import shutil
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_a_real_af_of_zero_is_preserved_as_a_measurement(self):
        """THE CONTROL THAT SEPARATES A PRESENCE FIX FROM A TRUTHINESS FIX.

        `AF=0` is a real reading: gnomAD covered this site and observed no
        alternate alleles. It must arrive as 0.0 -- NOT as None -- because
        that is genuine, PM2-relevant rarity evidence. A fix that made the
        unknown case work by mapping every falsy value to None would pass
        every test in classes A and B and fail right here."""
        gn = GnomadLookup(cfg=_local_cfg(self.tmpdir))
        with _TabixMocked(_vcf_line("AF=0;AC=0;AN=125000")):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIsInstance(hit, GnomadHit)
        self.assertIsNotNone(hit.af, "a measured zero is a measurement, not an absence")
        self.assertEqual(hit.af, 0.0)

    def test_a_measured_zero_still_awards_pm2(self):
        """The other half of the same control, at the observable level: the
        real zero must still reach PM2 and still count in its favour."""
        variant = {
            "chrom": CHROM,
            "pos": POS,
            "ref": REF,
            "alt": ALT,
            "gene_name": "BRCA1",
            "consequence": "missense_variant",
        }
        with _TabixMocked(_vcf_line("AF=0;AC=0;AN=125000")):
            with mock.patch("pipeline.gnomad.lookup.GnomadLookup.lookup_batch", return_value=None):
                r = run_acmg_evidence_batch(
                    [variant], _local_cfg(self.tmpdir), sample_id="TESTSAMPLE"
                )[0]
        self.assertIn(
            "PM2",
            r["criteria_met"],
            "a genuinely measured AF of 0 is rarity evidence and must still award PM2",
        )

    def test_a_real_nonzero_af_flows_through_unchanged(self):
        gn = GnomadLookup(cfg=_local_cfg(self.tmpdir))
        with _TabixMocked(_vcf_line("AF=0.15;AF_popmax=0.2;AC=1500;AN=10000")):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertEqual(hit.af, 0.15)
        self.assertEqual(hit.af_popmax, 0.2)
        self.assertEqual(hit.ac, 1500)
        self.assertEqual(hit.an, 10000)

    def test_api_populations_still_compute_a_real_popmax(self):
        """The `populations` loop must keep working when there IS data --
        the empty-list fix must not disable the computation itself."""
        gn = GnomadLookup(cfg=_api_cfg(self.tmpdir))
        payload = _api_payload(
            {
                "af": 0.01,
                "populations": [
                    {"id": "afr", "ac": 10, "an": 1000},  # 0.01
                    {"id": "eas", "ac": 50, "an": 1000},  # 0.05  <- popmax
                ],
            }
        )
        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        resp.json.return_value = payload
        with mock.patch("pipeline.gnomad.lookup.requests.post", return_value=resp):
            hit = gn.lookup(CHROM, POS, REF, ALT)
        self.assertAlmostEqual(hit.af_popmax, 0.05)

    def test_confirmed_absent_is_still_absent_not_unknown(self):
        """tabix returning nothing means the variant is genuinely not in the
        file. That is ABSENT -- the state PM2 exists for -- and must not be
        folded into the new unknown state."""
        gn = GnomadLookup(cfg=_local_cfg(self.tmpdir))
        with _TabixMocked(""):
            outcome = gn.lookup(CHROM, POS, REF, ALT)
        self.assertIs(outcome, GnomadLookupOutcome.ABSENT)

    def test_a_common_variant_still_denies_pm2(self):
        variant = {
            "chrom": CHROM,
            "pos": POS,
            "ref": REF,
            "alt": ALT,
            "gene_name": "BRCA1",
            "consequence": "missense_variant",
        }
        with _TabixMocked(_vcf_line("AF=0.15;AF_popmax=0.2;AC=1500;AN=10000")):
            with mock.patch("pipeline.gnomad.lookup.GnomadLookup.lookup_batch", return_value=None):
                r = run_acmg_evidence_batch(
                    [variant], _local_cfg(self.tmpdir), sample_id="TESTSAMPLE"
                )[0]
        self.assertNotIn("PM2", r["criteria_met"], "af=0.15 is common; PM2 must not fire")


# ---------------------------------------------------------------------------
# D. THE ENUM GUARD (T1-F4, folded into this work)
#
# Folded in here rather than carded separately because it is the same piece of
# work: HIGH 1 had to decide whether the PRESENT-with-unknown-AF fourth state
# belonged in `GnomadLookupOutcome`, and the answer was no PRECISELY BECAUSE
# the enum was unguarded -- adding a state to a type where every member is
# truthy is adding a state that silently reads as "yes". Deciding that and
# then leaving the enum unguarded would have been half a fix.
#
# Scope is GnomadLookupOutcome ONLY. The other six unguarded enums this repo's
# audit found stay carded and unruled; none of them is load-bearing here.
# ---------------------------------------------------------------------------


class TestTheOutcomeEnumHasNoTruthValue(unittest.TestCase):
    def test_every_member_refuses_to_be_a_boolean(self):
        """All three members, not a sample: the whole point is that they were
        INDISTINGUISHABLE in a boolean context, so a guard that covered only
        some of them would leave the collapse in place for the rest."""
        for member in GnomadLookupOutcome:
            with self.subTest(member=member.name):
                with self.assertRaises(TypeError):
                    bool(member)
                with self.assertRaises(TypeError):
                    if member:  # noqa: SIM103  -- the shape being forbidden
                        pass
                with self.assertRaises(TypeError):
                    if not member:
                        pass

    def test_the_message_names_the_type_and_the_form_to_use_instead(self):
        """A guard that just says 'TypeError' teaches nothing at the call site
        where it fires."""
        with self.assertRaises(TypeError) as ctx:
            bool(GnomadLookupOutcome.UNAVAILABLE)
        msg = str(ctx.exception)
        self.assertIn("GnomadLookupOutcome", msg)
        self.assertIn("is GnomadLookupOutcome.ABSENT", msg)
        self.assertIn("isinstance", msg)

    def test_it_catches_the_mistake_this_modules_own_example_used_to_show(self):
        """The module docstring used to demonstrate `if hit: print(hit.af)`.
        Both arms of the Union `lookup()` returns are unconditionally truthy,
        so that example read UNAVAILABLE as a successful hit -- the documented
        usage pattern WAS the bug class. The guard turns that from a silent
        wrong answer into a loud one."""
        result = GnomadLookupOutcome.UNAVAILABLE
        with self.assertRaises(TypeError):
            if result:
                self.fail("an unavailable lookup must never read as a hit")

    def test_the_comparisons_the_module_actually_uses_still_work(self):
        """The guard must not break the code around it. `lookup.py` narrows
        with `==` (:230, :244) and callers use `is`; neither consults
        `__bool__`."""
        self.assertIs(GnomadLookupOutcome.ABSENT, GnomadLookupOutcome("absent"))
        self.assertTrue(GnomadLookupOutcome.ABSENT == GnomadLookupOutcome.ABSENT)
        self.assertFalse(GnomadLookupOutcome.ABSENT == GnomadLookupOutcome.UNAVAILABLE)
        self.assertNotEqual(GnomadLookupOutcome.PRESENT, GnomadLookupOutcome.ABSENT)

    def test_hashing_and_membership_still_work(self):
        """`__hash__`/`__eq__`, never `__bool__` -- so dict keys and sets are
        unaffected. Pinned because a guard like this is exactly the kind of
        change that quietly breaks a lookup table somewhere else."""
        table = {
            GnomadLookupOutcome.ABSENT: "pm2 ok",
            GnomadLookupOutcome.UNAVAILABLE: "pm2 must not fire",
        }
        self.assertEqual(table[GnomadLookupOutcome.ABSENT], "pm2 ok")
        self.assertIn(GnomadLookupOutcome.UNAVAILABLE, table)
        self.assertNotIn(GnomadLookupOutcome.PRESENT, table)

    def test_the_hit_itself_is_deliberately_not_guarded(self):
        """CONTROL, and a boundary worth stating: the guard is on the ENUM, not
        on the whole Union. `GnomadHit` stays truthy, so `isinstance` remains
        the way to narrow -- a reader who assumes the hit is guarded too would
        be wrong."""
        hit = GnomadHit(af=None, af_popmax=None, ac=0, an=0, backend_used="test")
        self.assertTrue(bool(hit), "GnomadHit is a plain dataclass and stays truthy")

    def test_a_real_lookup_result_can_still_be_narrowed_the_documented_way(self):
        """End to end: the pattern the corrected docstring now shows must
        actually work against a real `lookup()` return."""
        import shutil
        import tempfile

        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        gn = GnomadLookup(cfg=_local_cfg(tmpdir))
        with _TabixMocked(""):
            result = gn.lookup(CHROM, POS, REF, ALT)
        self.assertFalse(isinstance(result, GnomadHit))
        self.assertIs(result, GnomadLookupOutcome.ABSENT)


if __name__ == "__main__":
    unittest.main()
