"""
Pins the two guarantees `PackageCheckStatus` makes at the TYPE level,
rather than by convention in a design document.

WHY THIS FILE EXISTS. The tri-state's whole purpose is that a check
which never ran cannot be reported as a package that is confirmed
missing. Two properties carry that:

  1. The type HAS NO TRUTH VALUE. `if not status:` cannot silently
     misread NOT_CHECKED, because it raises. No encoding survives
     Python's binary truthiness with three states -- a `str` enum makes
     all three truthy (so `if not x:` collapses to "always available",
     worse than the bool it replaced), and an IntEnum with
     NOT_CHECKED = 0 makes ABSENT truthy (so a confirmed-missing
     package reads as available). Removing truthiness from the type is
     the only answer that does not pick a least-bad misreading.

  2. It is JSON-SAFE BY CONSTRUCTION. json's encoder special-cases
     `isinstance(o, str)` before it consults any custom encoder,
     `__str__` or `__bool__`, and every member genuinely is a `str`.
     So the raise in (1) and JSON safety ride different protocols and
     never collide -- but only while the `str` mixin is there.

A PROTECTION NOBODY HAS WATCHED FIRE IS NOT KNOWN TO WORK. Both
properties below were confirmed failing against a deliberately broken
type before being kept: dropping `__bool__` makes the truthiness tests
pass-through silently, and dropping the `str` base makes json.dumps
raise `TypeError: Object of type PackageCheckStatus is not JSON
serializable`. Neither would be caught by any other test in this suite.
"""

import enum
import json
import unittest

from utils.auto_install import PackageCheckStatus


class TestNoTruthValue(unittest.TestCase):
    """`if x:` / `if not x:` must raise, for every member, always."""

    def test_bool_raises_for_present(self):
        with self.assertRaises(TypeError):
            bool(PackageCheckStatus.PRESENT)

    def test_bool_raises_for_absent(self):
        with self.assertRaises(TypeError):
            bool(PackageCheckStatus.ABSENT)

    def test_bool_raises_for_not_checked(self):
        with self.assertRaises(TypeError):
            bool(PackageCheckStatus.NOT_CHECKED)

    def test_the_actual_dangerous_shapes_raise(self):
        """Not `bool()` in the abstract -- the two lines a caller writes."""
        for member in PackageCheckStatus:
            with self.subTest(member=member.value):
                with self.assertRaises(TypeError):
                    if member:
                        pass
                with self.assertRaises(TypeError):
                    if not member:
                        pass

    def test_the_error_names_the_fix(self):
        """A raise a reader cannot act on just moves the confusion."""
        with self.assertRaises(TypeError) as caught:
            bool(PackageCheckStatus.NOT_CHECKED)
        message = str(caught.exception)
        self.assertIn("PackageCheckStatus.PRESENT", message)
        self.assertIn("no truth value", message)

    def test_explicit_comparison_still_works(self):
        """The raise must not cost the comparison it steers people to."""
        self.assertIs(PackageCheckStatus.PRESENT, PackageCheckStatus.PRESENT)
        self.assertEqual(PackageCheckStatus.PRESENT, PackageCheckStatus.PRESENT)
        self.assertNotEqual(PackageCheckStatus.PRESENT, PackageCheckStatus.ABSENT)
        self.assertIsNot(PackageCheckStatus.NOT_CHECKED, PackageCheckStatus.ABSENT)


class TestJsonSafeByConstruction(unittest.TestCase):
    def test_members_serialise_without_dot_value(self):
        payload = {m.name: m for m in PackageCheckStatus}
        self.assertEqual(
            json.loads(json.dumps(payload)),
            {"NOT_CHECKED": "not_checked", "ABSENT": "absent", "PRESENT": "present"},
        )

    def test_serialising_never_triggers_the_bool_raise(self):
        """The two guarantees must not be able to collide."""
        self.assertEqual(json.dumps(PackageCheckStatus.NOT_CHECKED), '"not_checked"')

    def test_the_str_base_is_what_makes_that_true(self):
        """
        Pins the mechanism, not just the outcome. If someone drops the
        `str` mixin the serialisation tests above fail with a
        TypeError -- this one says why in the failure itself.
        """
        self.assertIsInstance(PackageCheckStatus.PRESENT, str)
        self.assertIsInstance(PackageCheckStatus.PRESENT, enum.Enum)


class TestTheThreeStatesStayDistinct(unittest.TestCase):
    def test_exactly_three_members(self):
        self.assertEqual(
            [m.value for m in PackageCheckStatus],
            ["not_checked", "absent", "present"],
        )

    def test_not_checked_is_not_absent(self):
        """The entire point, stated as an assertion."""
        self.assertNotEqual(PackageCheckStatus.NOT_CHECKED, PackageCheckStatus.ABSENT)


if __name__ == "__main__":
    unittest.main()
