"""
Extends the `PackageCheckStatus` guarantee to the five older status
enums: `StageStatus`, `VersionStatus`, `ClinVarMatchStatus`,
`DbSNPMatchStatus`, `GeneResolutionStatus`.

FIX #12 (2026-08-31): `DbSNPMatchStatus` and `GeneResolutionStatus`
were found, alongside `ClinVarMatchStatus`, as the same tri-state
pattern by Andy while designing `PackageCheckStatus` -- explicitly
scoped OUT of that card and out of the original three-enum sweep
(different blast radius), then carded separately by the human's
ruling ("add to all three, close at three, sweep the two siblings as
a separate card"). This file's own parameterized-over-`_ENUMS` design
is what makes that sweep a one-line addition rather than new tests:
adding them to the tuple below extends every test in this file to
both without writing anything new.

WHY. Each of these exists to keep states that look alike from
collapsing into one. `provenance.py`'s own docstring says it outright --
`NOT_CONSULTED` and `UNKNOWN` are "deliberately two different states,
not one ... collapsing them would repeat the exact bug class
`pipeline/stage_schemas.py` was built to fix."

BUT EVERY MEMBER OF A `str` ENUM IS TRUTHY, because every value is a
non-empty string. So `if status:` is True for all of them and
`if not status:` is False for all of them -- truthiness collapses
exactly the distinctions these types were built to preserve, and does
it silently. A reader writing `if not version_status:` to mean "nothing
was recorded" gets a branch that never fires, for every input.

NO SITE DOES THIS TODAY. Checked across the whole non-test tree before
these were written: the only `if <name>status:` matches are
`confidence_engine.py:190` and `ps1_pm5/models.py:52`, and both are
plain ClinVar `Optional[str]` review-status strings, not these enums.
So this is a latent hazard, closed the same way `PackageCheckStatus`
closed it -- by removing the truth value rather than trusting everyone
to remember.

The `str` mixin stays, so JSON serialisation is unaffected: json's
encoder special-cases `isinstance(o, str)` before consulting any custom
encoder or `__bool__`, which is why the raise and JSON safety cannot
collide. Pinned below for each type.
"""

import enum
import json
import unittest

from database.clinvar_client import ClinVarMatchStatus
from database.dbsnp_client import DbSNPMatchStatus
from pipeline.clingen.utils import GeneResolutionStatus
from pipeline.provenance import VersionStatus
from pipeline.stage_schemas import StageStatus

_ENUMS = (StageStatus, VersionStatus, ClinVarMatchStatus, DbSNPMatchStatus, GeneResolutionStatus)


class TestNoneOfThemHasATruthValue(unittest.TestCase):
    def test_bool_raises_for_every_member_of_every_enum(self):
        for enum_cls in _ENUMS:
            for member in enum_cls:
                with self.subTest(enum=enum_cls.__name__, member=member.value):
                    with self.assertRaises(TypeError):
                        bool(member)

    def test_the_actual_dangerous_shapes_raise(self):
        """Not `bool()` in the abstract -- the two lines a caller writes."""
        for enum_cls in _ENUMS:
            for member in enum_cls:
                with self.subTest(enum=enum_cls.__name__, member=member.value):
                    with self.assertRaises(TypeError):
                        if member:
                            pass
                    with self.assertRaises(TypeError):
                        if not member:
                            pass

    def test_each_error_names_its_own_type(self):
        """
        A raise a reader cannot act on just moves the confusion. The
        message must name the type in front of them, not a generic one.
        """
        for enum_cls in _ENUMS:
            member = list(enum_cls)[0]
            with self.subTest(enum=enum_cls.__name__):
                with self.assertRaises(TypeError) as caught:
                    bool(member)
                message = str(caught.exception)
                self.assertIn(enum_cls.__name__, message)
                self.assertIn("no truth value", message)


class TestTheRaiseDoesNotCostAnythingElse(unittest.TestCase):
    def test_explicit_comparison_still_works(self):
        for enum_cls in _ENUMS:
            members = list(enum_cls)
            with self.subTest(enum=enum_cls.__name__):
                self.assertIs(members[0], members[0])
                self.assertEqual(members[0], members[0])
                if len(members) > 1:
                    self.assertNotEqual(members[0], members[1])

    def test_still_json_safe_without_dot_value(self):
        for enum_cls in _ENUMS:
            with self.subTest(enum=enum_cls.__name__):
                payload = {m.name: m for m in enum_cls}
                self.assertEqual(
                    json.loads(json.dumps(payload)),
                    {m.name: m.value for m in enum_cls},
                )

    def test_the_str_base_is_what_makes_that_true(self):
        for enum_cls in _ENUMS:
            member = list(enum_cls)[0]
            with self.subTest(enum=enum_cls.__name__):
                self.assertIsInstance(member, str)
                self.assertIsInstance(member, enum.Enum)

    def test_dict_lookup_still_works(self):
        """
        `provenance.py::RunProvenanceCollector.record` decides whether a
        new record may overwrite an existing one by looking each status
        up in `_STATUS_PRIORITY`. That is a dict lookup on the member,
        which uses `__hash__`/`__eq__` and never `__bool__` -- pinned
        here because breaking it would silently let a degraded query
        overwrite a captured real version.
        """
        from pipeline.provenance import _STATUS_PRIORITY

        for member in VersionStatus:
            with self.subTest(member=member.value):
                self.assertIsInstance(_STATUS_PRIORITY[member], int)


if __name__ == "__main__":
    unittest.main()
