"""
Tests for R10: the signing clinician's identity comes from the user record.

Human ruling 2026-09-13: a clinical report shows the signing clinician's NAME,
REGISTRATION NUMBER and HOSPITAL. `review/signoff.py::approve` took all three
as free text typed at sign-off time, with nothing tying them to the account
that signed -- so a report could print any identity at all, including a
colleague's, and no record could contradict it.

This suite covers the rendering half. The database half (the three columns on
the clinical user record, and the refusal when one is missing) lives in
`clinical/tests/test_r10_clinician_identity.py`.

WHAT IS ASSERTED, behaviourally, against real ReportLab renders:

  - a report signed from a user record shows that record's three values, in
    the PDFs and in the manifest, and records that they came from the record;
  - a typed-in value that CONTRADICTS the user record is refused rather than
    printed -- the property the whole change exists to establish;
  - an incomplete user record is refused, naming the missing field;
  - the typed-in path still works, unchanged, for anything not backed by a
    user record (the CLI, a filesystem-only deployment) -- the control that
    proves the existing behaviour is preserved rather than replaced.
"""

import json
import os
import tempfile
import unittest

from review import signoff as s
from utils.exceptions import SignoffError

from tests.test_signoff import _PYPDF_AVAILABLE, _all_pdf_text, _write_run

NAME = "Dr. Rajesh Sharma"
REG = "MCI-12345"
HOSPITAL = "AIIMS Delhi"

# Exactly the dict clinical/data_access.py::get_signing_identity returns.
USER_RECORD = {
    "user_id": "8f14e45f-ea2e-4f07-9c8d-000000000001",
    "full_name": NAME,
    "registration_number": REG,
    "hospital": HOSPITAL,
}


class TestClinicianIdentityFromUserRecord(unittest.TestCase):
    def test_identity_is_built_from_the_record_and_renders_the_three_values(self):
        identity = s.ClinicianIdentity.from_user_record(USER_RECORD)
        self.assertEqual(identity.full_name, NAME)
        self.assertEqual(identity.registration_number, REG)
        self.assertEqual(identity.hospital, HOSPITAL)
        physician = identity.physician_string()
        for value in (NAME, REG, HOSPITAL):
            self.assertIn(value, physician)

    def test_record_missing_registration_number_is_refused_and_names_it(self):
        record = dict(USER_RECORD, registration_number=None)
        with self.assertRaises(SignoffError) as ctx:
            s.ClinicianIdentity.from_user_record(record)
        self.assertIn("registration_number", str(ctx.exception))

    def test_record_with_a_blank_field_is_refused(self):
        """A blank prints as a signature with no credential, which is the defect."""
        record = dict(USER_RECORD, hospital="   ")
        with self.assertRaises(SignoffError) as ctx:
            s.ClinicianIdentity.from_user_record(record)
        self.assertIn("hospital", str(ctx.exception))


class TestApproveFromUserRecord(unittest.TestCase):
    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_signed_report_shows_the_records_name_registration_and_hospital(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, identity=s.ClinicianIdentity.from_user_record(USER_RECORD))

            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))
            self.assertIn(NAME, full_text)
            self.assertIn(REG, full_text)
            self.assertIn(HOSPITAL, full_text)
            self.assertNotIn("DRAFT", full_text)

    def test_manifest_records_the_three_values_and_where_they_came_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            manifest = s.approve(output_dir, identity=s.ClinicianIdentity.from_user_record(USER_RECORD))

            self.assertEqual(manifest["clinician_name"], NAME)
            self.assertEqual(manifest["reg_number"], REG)
            self.assertEqual(manifest["hospital"], HOSPITAL)
            self.assertEqual(manifest["identity_source"], "user_record")
            self.assertEqual(manifest["clinician_user_id"], USER_RECORD["user_id"])

            document = json.load(open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8"))
            for value in (NAME, REG, HOSPITAL):
                self.assertIn(value, document["reviewed_by"])

    def test_typed_in_values_that_contradict_the_record_are_refused(self):
        """
        The central guarantee: a report must not be able to show an identity
        that contradicts the user record. Nothing is written -- the refusal
        happens before any file is touched.
        """
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with self.assertRaises(SignoffError) as ctx:
                s.approve(
                    output_dir,
                    clinician_name="Dr. Someone Else",
                    reg_number=REG,
                    hospital=HOSPITAL,
                    identity=s.ClinicianIdentity.from_user_record(USER_RECORD),
                )
            self.assertIn("clinician_name", str(ctx.exception))
            self.assertFalse(os.path.exists(s._manifest_path(output_dir)))
            self.assertFalse(os.path.exists(s._patient_meta_path(output_dir)))

    def test_typed_in_values_that_agree_with_the_record_are_accepted(self):
        """CONTROL for the refusal above: agreement is not rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            manifest = s.approve(
                output_dir,
                clinician_name=NAME,
                reg_number=REG,
                hospital=HOSPITAL,
                identity=s.ClinicianIdentity.from_user_record(USER_RECORD),
            )
            self.assertEqual(manifest["identity_source"], "user_record")


class TestTypedInPathUnchanged(unittest.TestCase):
    """
    CONTROL: the filesystem-only deployment (review/cli.py, any run with no
    clinical user record behind it) behaves exactly as before.
    """

    def test_typed_in_approve_still_signs_and_marks_its_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            manifest = s.approve(output_dir, NAME, REG, HOSPITAL)
            self.assertEqual(manifest["clinician_name"], NAME)
            self.assertEqual(manifest["reg_number"], REG)
            self.assertEqual(manifest["hospital"], HOSPITAL)
            self.assertEqual(manifest["identity_source"], "typed_in")
            self.assertIsNone(manifest["clinician_user_id"])

            document = json.load(open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8"))
            self.assertEqual(document["review_status"], "reviewed")

    def test_typed_in_approve_with_a_field_omitted_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with self.assertRaises(SignoffError) as ctx:
                s.approve(output_dir, NAME, None, HOSPITAL)
            self.assertIn("reg_number", str(ctx.exception))
            self.assertFalse(os.path.exists(s._manifest_path(output_dir)))


if __name__ == "__main__":
    unittest.main()
