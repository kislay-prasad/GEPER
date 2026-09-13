"""
api/tests/test_signoff_clinical_link.py
───────────────────────────────────────

w117 commit 6: review/signoff.py's LINKED path, against a REAL PostgreSQL
(api/tests/conftest.py::clinical_conn -- disposable databases only) and the
REAL DataAccess, not a stand-in for it. The clinical enforcement these tests
lean on -- role checks, the state machine, the claim preconditions -- is the
production code's, so a test here cannot pass by agreeing with a fake.

THE DEFECT. api/submission_worker.py writes `clinical_link.json` into the
output directory of every run it records as a clinical interpretation, and
nothing read it. A clinician could `approve()` such a run, get a signed PDF
with their name on every page, and leave the CLINICAL REPORT -- what an
inspector, a LIMS and the amendment machinery all read -- still saying
'draft', with no reviewer claim, no approver_id, no approved_at and no
content_hash. Two records of one act, disagreeing, the authoritative one
wrong.

STILL ONE-PERSON SIGN-OFF (human ruling, 2026-09-13). Nothing here requires a
second clinician. The change is that the clinical record now SAYS one person
signed, through the claim kind that means exactly that ('sole_signatory',
commit 5) rather than 'accept', which means an independent concurrence by a
second clinician and would be a false statement here.

THE TWO CONTROLS, both of which must stay green:
  - TestUnlinkedRunIsUnchanged: a run with no clinical_link.json signs off
    and overrides exactly as it did before, with no credentials, no DSN and
    no database.
  - TestTheSignedPdfNeverLeadsTheClinicalRecord: every clinical refusal
    (missing credentials, missing reason, wrong password, a signatory without
    the Approver role) leaves the directory byte-for-byte as it was. That is
    the write-order rule as a test rather than as a comment.
"""

from __future__ import annotations

import datetime
import json
import os
import uuid

import pytest

from api.submission_worker import CLINICAL_LINK_FILENAME as WORKER_CLINICAL_LINK_FILENAME
from clinical.data_access import AuthenticationError, AuthorizationError, DataAccess, SystemClock
from review import signoff as s
from tests.test_signoff import _make_document
from utils.exceptions import SignoffError


# The signatory's R10 identity, as held by the USER RECORD. The tests that
# also type these three in are proving that agreeing values are accepted, not
# that the typed ones are used -- TestIdentityComesFromTheUserRecord proves
# which source wins.
SIGNATORY_NAME = "Dr. A. Sharma, MD"
SIGNATORY_REG = "MCI-12345"
SIGNATORY_HOSPITAL = "ABC Diagnostics"
HOSPITAL_B = "ABC Diagnostics"

# w119 fix 2's CLI signatory, whose password is stored as a real bcrypt hash.
# Its own registration number: users_org_registration_number_unique forbids
# reusing SIGNATORY_REG inside one organisation.
CLI_SIGNATORY_NAME = "Dr. C. Nair, MD"
CLI_SIGNATORY_REG = "MCI-77777"


class _PlainHasher:
    """The clinical layer's hasher protocol without bcrypt, which the geper
    environment does not install. Only these fixtures' own logins use it."""

    def hash(self, plaintext: str) -> str:
        return "plain$" + plaintext

    def verify(self, plaintext: str, hashed: str) -> bool:
        return hashed == "plain$" + plaintext


@pytest.fixture
def dao(clinical_conn):
    return DataAccess(clinical_conn, clock=SystemClock(), password_hasher=_PlainHasher())


def _login_with_roles(dao, conn, org_id, admin, email, roles):
    user_id = dao.create_user(org_id, email, "password")
    for role in roles:
        dao.assign_role(admin, user_id, role, basis="w117 test fixture role grant")
    return dao.login(email, org_id, "password")


@pytest.fixture
def clinical(dao, clinical_conn):
    """
    One organisation with a real FK chain down to a DRAFT report, plus the
    people this suite signs with.

    `signatory` holds BOTH Interpreter and Approver, which is what one-person
    sign-off means in the clinical layer's existing terms: submit_for_review
    has always required Interpreter and approval has always required Approver,
    and with one person those two requirements land on one identity. No check
    is relaxed to make that work -- `interpreter_only` exists to prove it.
    """
    conn = clinical_conn
    now = datetime.datetime(2026, 9, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
    org_id = dao.create_organisation("Org A")
    admin_id = dao.create_user(org_id, "admin@org-a.test", "password")
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (admin_id, org_id, admin_id, now),
        )
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", now),
        )
    conn.commit()
    admin = dao.login("admin@org-a.test", org_id, "password")

    patient_id = dao.create_patient(admin, "Test Patient", datetime.date(1990, 1, 1), "F")
    consent_id = dao.record_consent(admin, patient_id, "testing")
    order_id = dao.create_order(
        admin,
        patient_id=patient_id,
        test_id=test_id,
        consent_id=consent_id,
        required_scope="testing",
        priority="routine",
    )

    sample_id = uuid.uuid4()
    run_id = uuid.uuid4()
    vcf_id = uuid.uuid4()
    interp_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
            "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed')",
            (sample_id, order_id, org_id, now, admin.user_id),
        )
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (org_id, run_id, sample_id, now, admin.user_id),
        )
        cur.execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, vcf_id, run_id, "/tmp/x.vcf", "0" * 64, now, admin.user_id),
        )
        cur.execute(
            "INSERT INTO interpretations "
            "(org_id, id, vcf_id, run_document, submission_key, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, interp_id, vcf_id, json.dumps({"variants": []}), "sub-" + str(interp_id), now, admin.user_id),
        )
    conn.commit()
    report_id = dao.create_report(admin, interp_id)

    signatory = _login_with_roles(dao, conn, org_id, admin, "signatory@org-a.test", ("Interpreter", "Approver"))
    interpreter_only = _login_with_roles(dao, conn, org_id, admin, "interpreter@org-a.test", ("Interpreter",))
    # R10 identities, recorded by the Administrator (the only role that may).
    # A LINKED sign-off reads the three printed values from here, so an
    # account without them cannot sign -- TestIdentityComesFromTheUserRecord
    # covers that, with `unrecorded` below as its subject.
    dao.set_clinician_identity(admin, signatory.user_id, SIGNATORY_NAME, SIGNATORY_REG, SIGNATORY_HOSPITAL)
    dao.set_clinician_identity(admin, interpreter_only.user_id, "Dr. B. Interpreter, MD", "MCI-99999", HOSPITAL_B)

    return {
        "org_id": org_id,
        "interpretation_id": interp_id,
        "report_id": report_id,
        "admin": admin,
        "signatory": signatory,
        "interpreter_only": interpreter_only,
        # Holds both roles and can therefore sign the whole way through -- but
        # carries no name, registration number or hospital.
        "unrecorded": _login_with_roles(dao, conn, org_id, admin, "unrecorded@org-a.test", ("Interpreter", "Approver")),
    }


@pytest.fixture
def run_dir(tmp_path):
    """A completed, unlinked run: geper_results.json and nothing else."""
    output_dir = tmp_path / "run1"
    output_dir.mkdir()
    (output_dir / s.RESULTS_FILENAME).write_text(json.dumps(_make_document()), encoding="utf-8")
    return str(output_dir)


@pytest.fixture
def linked_run_dir(run_dir, clinical):
    """The same run, with the worker's clinical_link.json alongside it."""
    with open(os.path.join(run_dir, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "submission_id": "sub-1",
                "org_id": str(clinical["org_id"]),
                "order_id": str(uuid.uuid4()),
                "sample_id": str(uuid.uuid4()),
                "interpretation_id": str(clinical["interpretation_id"]),
                "report_id": str(clinical["report_id"]),
                "vcf_id": str(uuid.uuid4()),
                "submission_key": "key-1",
            },
            fh,
        )
    return run_dir


@pytest.fixture
def creds():
    return s.ClinicalCredentials(email="signatory@org-a.test", password="password")


def _document(output_dir):
    with open(os.path.join(output_dir, s.RESULTS_FILENAME), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _report_row(conn, report_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
    conn.commit()
    return row


def _claims(conn, report_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claim_type, variant_key, classification, actor_id, reason FROM reviewer_claims "
            'WHERE report_id = %s ORDER BY "timestamp"',
            (report_id,),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def _directory_state(output_dir):
    """Every file in the directory and its bytes -- so 'nothing was changed'
    can be asserted as a fact rather than as a couple of spot checks."""
    state = {}
    for name in sorted(os.listdir(output_dir)):
        with open(os.path.join(output_dir, name), "rb") as fh:
            state[name] = fh.read()
    return state


# ───────────────────────────────────────────────────────────────────────────
# THE FILENAME
# ───────────────────────────────────────────────────────────────────────────


def test_signoff_and_the_worker_agree_on_the_link_filename():
    """
    review/signoff.py names the file as a literal rather than importing it
    from api/submission_worker.py, to avoid an import edge from a
    filesystem-only module onto the API worker. This is what stops the two
    from drifting apart after a rename.
    """
    assert s.CLINICAL_LINK_FILENAME == WORKER_CLINICAL_LINK_FILENAME


# ───────────────────────────────────────────────────────────────────────────
# CONTROL: the unlinked run
# ───────────────────────────────────────────────────────────────────────────


class TestUnlinkedRunIsUnchanged:
    """
    A run with no clinical record behaves exactly as it did before w117. No
    credentials, no reason, no CLINICAL_DSN, no database touched.
    """

    def test_approve_still_signs_off_with_no_clinical_arguments(self, run_dir, monkeypatch):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        manifest = s.approve(run_dir, "Dr. A. Sharma, MD", "MCI-12345", "ABC Diagnostics")

        assert _document(run_dir)["review_status"] == "reviewed"
        assert os.path.exists(os.path.join(run_dir, s.MANIFEST_FILENAME))
        assert os.path.exists(os.path.join(run_dir, s.FULL_PDF_FILENAME))
        assert "clinical" not in manifest, (
            "an unlinked run's manifest must not carry a clinical block at all -- 'there is no "
            "clinical record' and 'there is one and we did not record it' must not read the same"
        )

    def test_override_still_works_with_no_clinical_arguments(self, run_dir, monkeypatch):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        record = s.override(run_dir, "17:43106534:C>A", "Likely Pathogenic", "Segregation data.", "dr@lab.test")

        assert _document(run_dir)["review_status"] == "overridden"
        assert record["new_classification"] == "Likely Pathogenic"
        assert "clinical" not in record


# ───────────────────────────────────────────────────────────────────────────
# THE JOIN: w117's linked sign-off meets R10's record-backed identity
# ───────────────────────────────────────────────────────────────────────────


class TestIdentityComesFromTheUserRecord:
    """
    A linked sign-off prints the name, registration number and hospital held
    by THE ACCOUNT THAT SIGNED, never the typed-in flags.

    The reason this is not merely tidier: `approve_report` writes
    `approver_id` on the clinical report, and R10's
    `signing_identity_for_report` reads the printed identity back out of that
    same column. So a linked sign-off already has two renderings of one
    signature -- the PDF's string and the clinical layer's answer. Typed-in
    values would let those two name different people, which is the same defect
    (signed artefact and clinical record disagreeing about one act) that the
    linked path exists to close, one field over.
    """

    def test_the_printed_identity_is_the_records_and_not_the_typed_one(self, linked_run_dir, dao, creds):
        """
        Nothing is typed at all, and the report still prints a full identity --
        which can only have come from the record.
        """
        manifest = s.approve(
            linked_run_dir,
            clinical_credentials=creds,
            clinical_reason="Sole signatory concurrence.",
            clinical_data_access=dao,
        )
        assert manifest["clinician_name"] == SIGNATORY_NAME
        assert manifest["reg_number"] == SIGNATORY_REG
        assert manifest["hospital"] == SIGNATORY_HOSPITAL
        assert manifest["identity_source"] == "user_record"
        assert (
            _document(linked_run_dir)["reviewed_by"]
            == f"{SIGNATORY_NAME}, Reg. No. {SIGNATORY_REG}, {SIGNATORY_HOSPITAL}"
        )

    def test_the_pdf_and_the_clinical_record_name_the_same_person(self, linked_run_dir, clinical, dao, creds):
        """
        THE JOIN, ASSERTED AS THE ONE FACT IT EXISTS FOR. What the manifest
        printed and what signing_identity_for_report answers for the same
        report must be the same three values and the same account.
        """
        manifest = s.approve(
            linked_run_dir,
            clinical_credentials=creds,
            clinical_reason="Sole signatory concurrence.",
            clinical_data_access=dao,
        )
        from_record = dao.signing_identity_for_report(clinical["signatory"], clinical["report_id"])

        assert manifest["clinician_name"] == from_record["full_name"]
        assert manifest["reg_number"] == from_record["registration_number"]
        assert manifest["hospital"] == from_record["hospital"]
        assert manifest["clinician_user_id"] == str(from_record["user_id"])
        assert manifest["clinician_user_id"] == manifest["clinical"]["approver_user_id"], (
            "the account the manifest credits and the account recorded as approver on the clinical "
            "report must be the same one"
        )

    def test_typed_values_that_agree_with_the_record_are_accepted(self, linked_run_dir, dao, creds):
        """R10's own rule, unchanged: agreement is not an error."""
        manifest = s.approve(
            linked_run_dir,
            SIGNATORY_NAME,
            SIGNATORY_REG,
            SIGNATORY_HOSPITAL,
            clinical_credentials=creds,
            clinical_reason="Sole signatory concurrence.",
            clinical_data_access=dao,
        )
        assert manifest["identity_source"] == "user_record"

    @pytest.mark.parametrize(
        "typed, field",
        [
            (("Dr. Someone Else", SIGNATORY_REG, SIGNATORY_HOSPITAL), "clinician_name"),
            ((SIGNATORY_NAME, "MCI-00000", SIGNATORY_HOSPITAL), "reg_number"),
            ((SIGNATORY_NAME, SIGNATORY_REG, "Some Other Hospital"), "hospital"),
        ],
    )
    def test_a_typed_value_contradicting_the_record_is_refused(
        self, linked_run_dir, clinical, clinical_conn, dao, creds, typed, field
    ):
        """
        Refused by R10's own _resolve_signing_identity, not by a second rule
        written for this path -- and refused BEFORE the claim, the approval and
        every file write.
        """
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match=field):
            s.approve(
                linked_run_dir,
                *typed,
                clinical_credentials=creds,
                clinical_reason="Sole signatory concurrence.",
                clinical_data_access=dao,
            )
        assert _directory_state(linked_run_dir) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"
        assert _claims(clinical_conn, clinical["report_id"]) == []

    def test_an_identity_object_contradicting_the_record_is_refused(
        self, linked_run_dir, clinical, clinical_conn, dao, creds
    ):
        """The same verdict when the caller passes a whole ClinicianIdentity:
        on a linked run the record is authoritative, full stop."""
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="registration_number"):
            s.approve(
                linked_run_dir,
                identity=s.ClinicianIdentity(SIGNATORY_NAME, "MCI-00000", SIGNATORY_HOSPITAL),
                clinical_credentials=creds,
                clinical_reason="Sole signatory concurrence.",
                clinical_data_access=dao,
            )
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, clinical["report_id"]) == []

    def test_an_account_with_no_recorded_identity_cannot_sign_a_linked_run(
        self, linked_run_dir, clinical, clinical_conn, dao
    ):
        """
        R10 refuses rather than printing "Reg. No.: not provided". On a linked
        run that refusal must arrive before the CLINICAL writes as well as the
        file writes -- approving a report the platform then cannot print a
        signature for would be the worst of both.
        """
        before = _directory_state(linked_run_dir)
        with pytest.raises(Exception) as exc:
            s.approve(
                linked_run_dir,
                clinical_credentials=s.ClinicalCredentials("unrecorded@org-a.test", "password"),
                clinical_reason="Sole signatory concurrence.",
                clinical_data_access=dao,
            )
        assert "full_name" in str(exc.value) and "registration_number" in str(exc.value)
        assert _directory_state(linked_run_dir) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"
        assert _claims(clinical_conn, clinical["report_id"]) == [], (
            "the identity must be read BEFORE the sole_signatory claim -- an account that cannot be "
            "printed must not leave a concurrence behind it"
        )

    def test_an_unlinked_run_still_takes_the_typed_identity(self, run_dir, monkeypatch):
        """
        CONTROL. R10's filesystem-only path is untouched: no record to read,
        typed values used, identity_source says so.
        """
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        manifest = s.approve(run_dir, "Dr. Typed In", "MCI-55555", "Some Lab")
        assert manifest["identity_source"] == "typed_in"
        assert manifest["clinician_name"] == "Dr. Typed In"
        assert manifest["clinician_user_id"] is None


# ───────────────────────────────────────────────────────────────────────────
# The linked sign-off
# ───────────────────────────────────────────────────────────────────────────


class TestLinkedApprove:
    def test_the_clinical_report_is_approved_by_the_person_who_signed(
        self, linked_run_dir, clinical, clinical_conn, dao, creds
    ):
        manifest = s.approve(
            linked_run_dir,
            "Dr. A. Sharma, MD",
            "MCI-12345",
            "ABC Diagnostics",
            clinical_credentials=creds,
            clinical_reason="Sole signatory: re-derived every call on this report myself.",
            clinical_data_access=dao,
        )

        state, approver_id, approved_at, content_hash = _report_row(clinical_conn, clinical["report_id"])
        assert state == "approved"
        assert approver_id == clinical["signatory"].user_id
        assert approved_at is not None and content_hash is not None
        assert manifest["clinical"]["report_id"] == str(clinical["report_id"])
        assert manifest["clinical"]["approver_user_id"] == str(clinical["signatory"].user_id)

    def test_the_concurrence_is_recorded_as_sole_signatory_not_accept(
        self, linked_run_dir, clinical, clinical_conn, dao, creds
    ):
        """
        THE RULING. 'accept' means an independent concurrence by a SECOND
        clinician; one person signing alone must not write that row.
        """
        s.approve(
            linked_run_dir,
            "Dr. A. Sharma, MD",
            "MCI-12345",
            "ABC Diagnostics",
            clinical_credentials=creds,
            clinical_reason="Sole signatory: re-derived every call on this report myself.",
            clinical_data_access=dao,
        )
        rows = _claims(clinical_conn, clinical["report_id"])
        assert len(rows) == 1
        claim_type, variant_key, classification, actor_id, reason = rows[0]
        assert claim_type == "sole_signatory"
        assert variant_key is None and classification is None
        assert actor_id == clinical["signatory"].user_id
        assert reason == "Sole signatory: re-derived every call on this report myself."

    def test_the_files_are_signed_too(self, linked_run_dir, dao, creds):
        """The file half is unchanged: this is an ADDITION to approve(), not a
        replacement of it."""
        s.approve(
            linked_run_dir,
            "Dr. A. Sharma, MD",
            "MCI-12345",
            "ABC Diagnostics",
            clinical_credentials=creds,
            clinical_reason="Sole signatory concurrence.",
            clinical_data_access=dao,
        )
        document = _document(linked_run_dir)
        assert document["review_status"] == "reviewed"
        assert document["reviewed_by"] == "Dr. A. Sharma, MD, Reg. No. MCI-12345, ABC Diagnostics"
        assert os.path.exists(os.path.join(linked_run_dir, s.MANIFEST_FILENAME))
        assert os.path.exists(os.path.join(linked_run_dir, s.FULL_PDF_FILENAME))


class TestTheSignedPdfNeverLeadsTheClinicalRecord:
    """
    WRITE ORDER, AS A TEST. Every clinical refusal must leave the output
    directory byte-for-byte as it was -- no rewritten geper_results.json, no
    regenerated PDF, no manifest. The state this forbids is a signed PDF
    existing while the clinical report still says 'draft'.
    """

    def _assert_nothing_happened(self, before, output_dir, conn, report_id):
        assert _directory_state(output_dir) == before, "a refused sign-off changed files on disk"
        assert _report_row(conn, report_id)[0] == "draft"
        assert _claims(conn, report_id) == []

    def test_a_linked_run_without_credentials_is_refused(self, linked_run_dir, clinical, clinical_conn, dao):
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="authenticated person"):
            s.approve(
                linked_run_dir,
                "Dr. A. Sharma, MD",
                "MCI-12345",
                "ABC Diagnostics",
                clinical_reason="Sole signatory concurrence.",
                clinical_data_access=dao,
            )
        self._assert_nothing_happened(before, linked_run_dir, clinical_conn, clinical["report_id"])

    @pytest.mark.parametrize("bad_reason", [None, "", "   "])
    def test_a_linked_run_without_a_reason_is_refused(
        self, linked_run_dir, clinical, clinical_conn, dao, creds, bad_reason
    ):
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="reason"):
            s.approve(
                linked_run_dir,
                "Dr. A. Sharma, MD",
                "MCI-12345",
                "ABC Diagnostics",
                clinical_credentials=creds,
                clinical_reason=bad_reason,
                clinical_data_access=dao,
            )
        self._assert_nothing_happened(before, linked_run_dir, clinical_conn, clinical["report_id"])

    def test_a_wrong_password_is_refused(self, linked_run_dir, clinical, clinical_conn, dao):
        before = _directory_state(linked_run_dir)
        with pytest.raises(AuthenticationError):
            s.approve(
                linked_run_dir,
                "Dr. A. Sharma, MD",
                "MCI-12345",
                "ABC Diagnostics",
                clinical_credentials=s.ClinicalCredentials("signatory@org-a.test", "wrong-password"),
                clinical_reason="Sole signatory concurrence.",
                clinical_data_access=dao,
            )
        self._assert_nothing_happened(before, linked_run_dir, clinical_conn, clinical["report_id"])

    def test_a_signatory_without_the_approver_role_is_refused(self, linked_run_dir, clinical, clinical_conn, dao):
        """
        ENFORCEMENT IS NOT RELAXED FOR THE ONE-PERSON PATH. An Interpreter who
        is not an Approver still cannot approve, and the refusal arrives before
        any PDF is signed.
        """
        before = _directory_state(linked_run_dir)
        with pytest.raises(AuthorizationError):
            s.approve(
                linked_run_dir,
                "Dr. B. Interpreter, MD",
                "MCI-99999",
                "ABC Diagnostics",
                clinical_credentials=s.ClinicalCredentials("interpreter@org-a.test", "password"),
                clinical_reason="Sole signatory concurrence.",
                clinical_data_access=dao,
            )
        assert _directory_state(linked_run_dir) == before, "a refused sign-off changed files on disk"
        # The claim and the submission DID happen -- they are legitimate acts by
        # an Interpreter -- and the report stopped at 'under_review'. What must
        # not exist is a signed artefact, and none does.
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "under_review"
        assert not os.path.exists(os.path.join(linked_run_dir, s.MANIFEST_FILENAME))

    def test_an_unreadable_link_file_is_refused_rather_than_treated_as_unlinked(self, run_dir, dao):
        """
        The dangerous fallback: a corrupt link file silently demoting a LINKED
        run to the unlinked path, which signs off without touching the clinical
        record at all.
        """
        with open(os.path.join(run_dir, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        before = _directory_state(run_dir)
        with pytest.raises(SignoffError, match="linked to a clinical record"):
            s.approve(run_dir, "Dr. A. Sharma, MD", "MCI-12345", "ABC Diagnostics", clinical_data_access=dao)
        assert _directory_state(run_dir) == before

    def test_a_link_file_naming_no_report_is_refused(self, run_dir, clinical, dao, creds):
        with open(os.path.join(run_dir, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "org_id": str(clinical["org_id"]),
                    "interpretation_id": str(clinical["interpretation_id"]),
                    "report_id": None,
                },
                fh,
            )
        before = _directory_state(run_dir)
        with pytest.raises(SignoffError, match="report_id"):
            s.approve(
                run_dir,
                "Dr. A. Sharma, MD",
                "MCI-12345",
                "ABC Diagnostics",
                clinical_credentials=creds,
                clinical_reason="Sole signatory concurrence.",
                clinical_data_access=dao,
            )
        assert _directory_state(run_dir) == before


# ───────────────────────────────────────────────────────────────────────────
# The linked override
# ───────────────────────────────────────────────────────────────────────────


class TestLinkedOverrideBeforeApproval:
    def test_it_records_a_disagreement_claim_by_the_same_person(
        self, linked_run_dir, clinical, clinical_conn, dao, creds
    ):
        record = s.override(
            linked_run_dir,
            "17:43106534:C>A",
            "Likely Pathogenic",
            "Segregation data in two affected relatives.",
            "signatory@org-a.test",
            clinical_credentials=creds,
            clinical_data_access=dao,
        )

        rows = _claims(clinical_conn, clinical["report_id"])
        assert len(rows) == 1
        claim_type, variant_key, classification, actor_id, reason = rows[0]
        assert claim_type == "disagree"
        assert variant_key == "17-43106534-C-A", (
            "the clinical layer identifies a variant by the engine's chrom-pos-ref-alt key; the "
            "CLI's chrom:pos:ref>alt form must be converted, not stored verbatim"
        )
        assert classification == "Likely Pathogenic"
        assert actor_id == clinical["signatory"].user_id
        assert reason == "Segregation data in two affected relatives."
        assert record["clinical"]["claim_type"] == "disagree"

    def test_the_file_change_still_happens(self, linked_run_dir, dao, creds):
        s.override(
            linked_run_dir,
            "17:43106534:C>A",
            "Likely Pathogenic",
            "Segregation data.",
            "signatory@org-a.test",
            clinical_credentials=creds,
            clinical_data_access=dao,
        )
        document = _document(linked_run_dir)
        assert document["review_status"] == "overridden"
        variant = s.find_variant(document, "17", 43106534, "C", "A")
        assert variant["overrides"][-1]["new_classification"] == "Likely Pathogenic"

    def test_the_pipelines_own_classification_is_not_replaced(self, linked_run_dir, dao, creds):
        """An override is a claim beside the pipeline's call, never over it."""
        s.override(
            linked_run_dir,
            "17:43106534:C>A",
            "Likely Pathogenic",
            "Segregation data.",
            "signatory@org-a.test",
            clinical_credentials=creds,
            clinical_data_access=dao,
        )
        variant = s.find_variant(_document(linked_run_dir), "17", 43106534, "C", "A")
        assert variant["candidate_interpretation"]["acmg_classification"]["classification"] == "Pathogenic"

    def test_without_credentials_it_is_refused_and_nothing_changes(self, linked_run_dir, clinical, clinical_conn, dao):
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="authenticated person"):
            s.override(
                linked_run_dir,
                "17:43106534:C>A",
                "Likely Pathogenic",
                "Segregation data.",
                "signatory@org-a.test",
                clinical_data_access=dao,
            )
        assert _directory_state(linked_run_dir) == before
        assert _claims(clinical_conn, clinical["report_id"]) == []


class TestLinkedOverrideAfterApprovalIsRefused:
    """
    An approved report's content_hash attests to exactly what was approved.
    Changing what sits underneath it is the ISO 15189 7.5 nonconformance the
    platform exists to DETECT, not a workflow -- so an override after approval
    is refused and pointed at the amendment path.
    """

    @pytest.fixture
    def approved(self, linked_run_dir, dao, creds):
        s.approve(
            linked_run_dir,
            "Dr. A. Sharma, MD",
            "MCI-12345",
            "ABC Diagnostics",
            clinical_credentials=creds,
            clinical_reason="Sole signatory concurrence.",
            clinical_data_access=dao,
        )
        return linked_run_dir

    def test_it_is_refused_and_names_the_amendment_path(self, approved, dao, creds):
        with pytest.raises(SignoffError, match="AMENDMENT"):
            s.override(
                approved,
                "17:43106534:C>A",
                "Likely Pathogenic",
                "Segregation data.",
                "signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )

    def test_nothing_on_disk_changes(self, approved, dao, creds):
        before = _directory_state(approved)
        with pytest.raises(SignoffError):
            s.override(
                approved,
                "17:43106534:C>A",
                "Likely Pathogenic",
                "Segregation data.",
                "signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        assert _directory_state(approved) == before, (
            "the refusal must arrive BEFORE the run document, the PDFs and the Markdown are "
            "rewritten underneath an approved report"
        )

    def test_no_claim_is_appended_to_the_approved_report(self, approved, clinical, clinical_conn, dao, creds):
        """
        A claim appended after approval is content the stored content_hash does
        not cover, which verify_report_integrity would then report as a
        nonconformance. The refusal stops it being created at all.
        """
        before = _claims(clinical_conn, clinical["report_id"])
        with pytest.raises(SignoffError):
            s.override(
                approved,
                "17:43106534:C>A",
                "Likely Pathogenic",
                "Segregation data.",
                "signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        assert _claims(clinical_conn, clinical["report_id"]) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "approved"

    def test_the_approved_report_still_verifies(self, approved, clinical, dao, creds):
        """The end the whole refusal serves: the signed report's integrity check
        still passes afterwards."""
        with pytest.raises(SignoffError):
            s.override(
                approved,
                "17:43106534:C>A",
                "Likely Pathogenic",
                "Segregation data.",
                "signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        assert dao.verify_report_integrity(clinical["signatory"], clinical["report_id"]) is True


# ───────────────────────────────────────────────────────────────────────────
# The CLI's credential handling
# ───────────────────────────────────────────────────────────────────────────


class TestLinkedWithdrawCannotLeaveTheRecordSayingApproved:
    """
    w119 FIX 1. `withdraw()` had no path to the clinical record in its
    signature at all, so on a LINKED run it removed the manifest and reset
    `review_status` to 'draft' while the CLINICAL REPORT still said
    'approved'.

    THE HUMAN'S RULING, which sets the bar for why this one is worse than the
    divergence w117 closed: "a withdrawal is a deliberate safety act, so a
    record that still says approved contradicts a clinician's explicit
    decision rather than lagging a routine change."

    THE CLINICAL SIDE OF A WITHDRAWAL IS A REFUSAL, NOT A WRITE, and that is
    a reading of the clinical layer rather than a preference:
    clinical/data_access.py has no unapprove/retract method;
    reports.approver_id/approved_at/content_hash are write-once; and
    enforce_content_immutability() in clinical/schema.sql REFUSES to move an
    approved report's state anywhere but 'released'. reviewer_claims'
    claim_type CHECK has no kind meaning "retracted" either. So the
    divergence is closed by never creating it -- refuse before any file
    change, exactly as override()-after-approval already does -- rather than
    by a state transition the schema forbids. Giving the clinical layer a
    real retraction is a schema and governance decision and is NOT taken
    here.
    """

    @pytest.fixture
    def approved(self, linked_run_dir, dao, creds):
        s.approve(
            linked_run_dir,
            clinical_credentials=creds,
            clinical_reason="Sole signatory concurrence.",
            clinical_data_access=dao,
        )
        return linked_run_dir

    def test_the_two_records_never_disagree_after_a_withdrawal_attempt(
        self, approved, clinical, clinical_conn, monkeypatch
    ):
        """
        THE DEFECT ITSELF, ASSERTED BEHAVIOURALLY AND THROUGH THE OLD
        THREE-ARGUMENT CALL -- so this is red against the pre-fix code for a
        reason that is nothing to do with a signature: before the fix this
        call SUCCEEDED, leaving geper_results.json saying 'draft' while the
        clinical report said 'approved'. After the fix the same call is
        refused and both records stand.

        The assertion is the invariant, not the mechanism: the files must
        never end up retracted while the clinical record still says approved.
        """
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        try:
            s.withdraw(approved, reason="Signed off in error.", actor="signatory@org-a.test")
        except SignoffError:
            pass  # refusing is one legitimate outcome; leaving a lie is not

        file_status = _document(approved)["review_status"]
        report_state = _report_row(clinical_conn, clinical["report_id"])[0]
        assert not (file_status != "reviewed" and report_state == "approved"), (
            f"the run's files now say {file_status!r} while its clinical report still says "
            f"{report_state!r} -- a withdrawal is a deliberate safety act, and a record that "
            "still says approved afterwards contradicts a clinician's explicit decision"
        )

    def test_it_is_refused_and_the_clinical_report_is_still_approved(
        self, approved, clinical, clinical_conn, dao, creds
    ):
        """
        THE DEFECT, AS A TEST. Before this fix `withdraw()` returned happily
        here and the assertion that followed -- clinical report still
        'approved' while the files say 'draft' -- was the contradiction.
        """
        with pytest.raises(SignoffError):
            s.withdraw(
                approved,
                reason="Signed off in error.",
                actor="signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "approved"

    def test_nothing_on_disk_changes(self, approved, dao, creds):
        before = _directory_state(approved)
        with pytest.raises(SignoffError):
            s.withdraw(
                approved,
                reason="Signed off in error.",
                actor="signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        assert _directory_state(approved) == before, (
            "the refusal must arrive BEFORE the manifest is removed, review_status is reset and "
            "the three reports are regenerated -- a withdrawn sign-off standing in front of an "
            "'approved' clinical record is the state this refusal exists to prevent"
        )
        assert _document(approved)["review_status"] == "reviewed"
        assert os.path.exists(os.path.join(approved, s.MANIFEST_FILENAME))

    def test_the_refusal_says_why_rather_than_inventing_a_transition(self, approved, dao, creds):
        """
        PINNED POSITIVELY, not merely by absence: the message must actually
        SAY that an approval cannot be retracted and that the amendment path
        is not yet available. An assertion that it lacks some wrong word
        would pass against an empty string.
        """
        with pytest.raises(SignoffError) as exc:
            s.withdraw(
                approved,
                reason="Signed off in error.",
                actor="signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        message = str(exc.value)
        assert "no way to retract an approval" in message
        assert "AMENDMENT" in message
        assert "NOT YET AVAILABLE" in message
        assert "Nothing on disk has been changed." in message

    def test_a_linked_withdraw_without_credentials_is_refused(self, approved, clinical, clinical_conn, dao):
        """
        Same rule as approve(): reading the clinical report's state is an
        authenticated act, and "I could not look" must not read the same as
        "there was nothing to look at".
        """
        before = _directory_state(approved)
        with pytest.raises(SignoffError, match="authenticated person"):
            s.withdraw(
                approved,
                reason="Signed off in error.",
                actor="signatory@org-a.test",
                clinical_data_access=dao,
            )
        assert _directory_state(approved) == before
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "approved"

    def test_a_linked_run_whose_report_is_not_approved_is_withdrawn_normally(
        self, run_dir, clinical, clinical_conn, dao, creds, monkeypatch
    ):
        """
        NOT A BLANKET BAN ON WITHDRAWING LINKED RUNS. Where the clinical
        record carries no approval there is nothing to contradict, so the
        file-side retraction leaves the two records agreeing and proceeds.

        Reached here the way it is reached in practice: a directory signed
        off before it was linked, which the link file is then added to.
        """
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        s.approve(run_dir, "Dr. Typed In", "MCI-55555", "Some Lab")
        with open(os.path.join(run_dir, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "org_id": str(clinical["org_id"]),
                    "interpretation_id": str(clinical["interpretation_id"]),
                    "report_id": str(clinical["report_id"]),
                },
                fh,
            )

        result = s.withdraw(
            run_dir,
            reason="Signed off in error.",
            actor="signatory@org-a.test",
            clinical_credentials=creds,
            clinical_data_access=dao,
        )
        assert result["clinical_report_state"] == "draft"
        assert _document(run_dir)["review_status"] == "draft"
        assert not os.path.exists(os.path.join(run_dir, s.MANIFEST_FILENAME))
        assert _report_row(clinical_conn, clinical["report_id"])[0] == "draft"

    def test_the_cli_hands_withdraw_the_clinical_credentials(self, tmp_path, monkeypatch):
        """
        The CLI half of fix 1: `withdraw` now takes the same clinical flags
        approve/override take, and main() actually forwards them. Captured at
        the call rather than asserted on the parser, because a flag that
        parses and is then dropped is the bug this guards.
        """
        import review.cli as cli

        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", "password")
        captured = {}

        def _fake_withdraw(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        monkeypatch.setattr(cli, "_withdraw", _fake_withdraw)
        assert (
            cli.main(
                [
                    "withdraw",
                    "--output-dir",
                    str(tmp_path),
                    "--reason",
                    "Signed off in error.",
                    "--actor",
                    "signatory@org-a.test",
                    "--clinical-email",
                    "signatory@org-a.test",
                ]
            )
            == 0
        )
        assert captured["clinical_credentials"] == s.ClinicalCredentials(
            email="signatory@org-a.test", password="password", totp_code=None
        )

    def test_an_unlinked_withdraw_is_unchanged(self, run_dir, monkeypatch):
        """
        THE CONTROL. No clinical_link.json: no credentials, no CLINICAL_DSN,
        no database, and the withdrawal behaves exactly as it did before
        w119 -- including carrying no clinical key in its result, because
        "there is no clinical record" and "there is one and we did not look"
        must not read the same.
        """
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        s.approve(run_dir, "Dr. Typed In", "MCI-55555", "Some Lab")

        result = s.withdraw(run_dir, reason="Signed off in error.", actor="dr@lab.test")

        assert result["previous_review_status"] == "reviewed"
        assert "clinical_report_state" not in result
        assert _document(run_dir)["review_status"] == "draft"
        assert not os.path.exists(os.path.join(run_dir, s.MANIFEST_FILENAME))
        assert os.path.exists(os.path.join(run_dir, s.FULL_PDF_FILENAME))


class TestTheRefusalsNameOnlyThingsAUserCanReach:
    """
    w119 FIX 3. The override-after-approval refusal used to name
    `clinical.data_access::create_amendment`. NO COMMAND REACHES IT -- it is
    a DataAccess method with no CLI subcommand and no HTTP route anywhere in
    this repository.

    THE HUMAN'S RULING: "A refusal naming a command a user can't reach is a
    false claim in user-facing text, and worse than a stale docstring because
    it arrives when someone is trying to act. Say the amendment path isn't yet
    available rather than naming something that doesn't exist."

    NOT A CANNOT-FAIL TEST. "the message does not contain create_amendment"
    passes against an empty string, against a refusal that never fires and
    against a message that says nothing useful. So the positive wording is
    pinned too, and the message is taken from a refusal that actually
    happened.
    """

    @pytest.fixture
    def approved(self, linked_run_dir, dao, creds):
        s.approve(
            linked_run_dir,
            clinical_credentials=creds,
            clinical_reason="Sole signatory concurrence.",
            clinical_data_access=dao,
        )
        return linked_run_dir

    def test_the_override_refusal_does_not_name_an_unreachable_command(self, approved, clinical, dao, creds):
        with pytest.raises(SignoffError) as exc:
            s.override(
                approved,
                "17:43106534:C>A",
                "Likely Pathogenic",
                "Segregation data.",
                "signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        message = str(exc.value)
        # The message really is the refusal, and really is non-empty -- so the
        # absence assertion below is about wording and not about nothing.
        assert str(clinical["report_id"]) in message
        assert "already 'approved'" in message
        # POSITIVE: what it must say instead.
        assert "AMENDMENT" in message
        assert "NOT YET AVAILABLE" in message
        assert "no command here that issues one" in message
        # NEGATIVE: the false claim it used to make.
        assert "create_amendment" not in message
        assert "data_access::" not in message

    def test_the_withdraw_refusal_names_no_unreachable_command_either(self, approved, dao, creds):
        """The message added by fix 1 is held to the same rule, so the defect
        is not reintroduced one function over."""
        with pytest.raises(SignoffError) as exc:
            s.withdraw(
                approved,
                reason="Signed off in error.",
                actor="signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        message = str(exc.value)
        assert "AMENDMENT" in message and "NOT YET AVAILABLE" in message
        assert "create_amendment" not in message
        assert "data_access::" not in message


class TestTheCliDrivesARealClinicalConnection:
    """
    w119 FIX 2. Two things had never been executed by any test:

      - `review/signoff.py::_connect_clinical_data_access()`, because every
        existing test injects `clinical_data_access=dao`;
      - `review/cli.py::main()` against a LINKED run at all -- only
        `build_arg_parser` and `_clinical_credentials` were covered.

    ONE test covers both: the CLI is driven with argv, nothing is injected,
    and CLINICAL_DSN points at this suite's own disposable database, so the
    real connect path builds the real DataAccess and the real login runs.

    THE CLINICAL_DSN REFUSAL EXISTS IN CODE AND WAS UNTESTED -- it is NOT
    missing, and the distinction matters because "untested" and "missing"
    have different remediations. It is pinned below as an untested-but-present
    path.

    The password is a REAL bcrypt hash here, not the suite's _PlainHasher:
    the CLI's own DataAccess is constructed with the production default
    hasher, so a fixture user whose stored hash the default hasher cannot
    verify would never get past login and the connect path would prove
    nothing.

    The connection this opens is not closed. That leak is ACCEPTED by the
    human and is deliberately not fixed here.
    """

    @pytest.fixture
    def bcrypt_signatory(self, clinical, clinical_conn, dao):
        from clinical.data_access import BcryptHasher

        bcrypt_dao = DataAccess(clinical_conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))
        email = "cli-signatory@org-a.test"
        user_id = bcrypt_dao.create_user(clinical["org_id"], email, "password")
        for role in ("Interpreter", "Approver"):
            dao.assign_role(clinical["admin"], user_id, role, basis="w119 CLI end-to-end fixture role grant")
        # A registration number of its own: users_org_registration_number_unique
        # means this account cannot reuse the other signatory's.
        dao.set_clinician_identity(clinical["admin"], user_id, CLI_SIGNATORY_NAME, CLI_SIGNATORY_REG, HOSPITAL_B)
        clinical_conn.commit()
        return email

    def test_cli_main_signs_a_linked_run_over_a_real_clinical_dsn(
        self, linked_run_dir, clinical, clinical_conn, clinical_dsn, bcrypt_signatory, monkeypatch
    ):
        from review.cli import main as cli_main

        monkeypatch.setenv("CLINICAL_DSN", clinical_dsn)
        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", "password")
        # The CLI opens its OWN connection: this one must not be sitting on
        # locks the CLI would then wait for.
        clinical_conn.commit()

        exit_code = cli_main(
            [
                "approve",
                "--output-dir",
                linked_run_dir,
                "--clinical-email",
                bcrypt_signatory,
                "--reason",
                "Sole signatory concurrence, via the CLI.",
            ]
        )

        assert exit_code == 0, "the CLI's real connect path failed to sign a linked run"

        # The clinical half really happened, through a connection this test
        # never handed in.
        state, approver_id, approved_at, content_hash = _report_row(clinical_conn, clinical["report_id"])
        assert state == "approved"
        assert approved_at is not None and content_hash is not None
        claims = _claims(clinical_conn, clinical["report_id"])
        assert len(claims) == 1 and claims[0][0] == "sole_signatory"
        assert claims[0][4] == "Sole signatory concurrence, via the CLI."
        assert approver_id == claims[0][3]

        # ... and so did the file half, with the identity read from the
        # signing account's record rather than from any flag (none was given).
        document = _document(linked_run_dir)
        assert document["review_status"] == "reviewed"
        assert document["reviewed_by"] == f"{CLI_SIGNATORY_NAME}, Reg. No. {CLI_SIGNATORY_REG}, {HOSPITAL_B}"
        with open(os.path.join(linked_run_dir, s.MANIFEST_FILENAME), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        assert manifest["identity_source"] == "user_record"
        assert manifest["clinical"]["approver_user_id"] == str(approver_id)

    def test_the_unset_clinical_dsn_refusal_is_present_and_now_exercised(self, linked_run_dir, creds, monkeypatch):
        """
        UNTESTED-BUT-PRESENT, now tested. This refusal has always existed in
        `_connect_clinical_data_access`; nothing had ever run it, because
        every other test injects a DataAccess and never reaches the connect
        path. It is not a missing check.
        """
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        before = _directory_state(linked_run_dir)
        with pytest.raises(SignoffError, match="CLINICAL_DSN"):
            s.approve(
                linked_run_dir,
                clinical_credentials=creds,
                clinical_reason="Sole signatory concurrence.",
            )
        assert _directory_state(linked_run_dir) == before


class TestCliCredentials:
    """
    THE PASSWORD IS NEVER AN ARGV ELEMENT. It is named indirectly, by the
    environment variable holding it, because a password on a command line is
    readable by every process on the machine and is written into shell history.
    """

    def test_there_is_no_password_argument(self):
        from review.cli import build_arg_parser

        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "approve",
                    "--output-dir",
                    "x",
                    "--clinician-name",
                    "n",
                    "--reg-number",
                    "r",
                    "--hospital",
                    "h",
                    "--clinical-password",
                    "hunter2",
                ]
            )

    def test_credentials_are_read_from_the_named_environment_variable(self, monkeypatch):
        from review.cli import _clinical_credentials, build_arg_parser

        monkeypatch.setenv("MY_CLINICAL_PW", "s3cret")
        args = build_arg_parser().parse_args(
            [
                "approve",
                "--output-dir",
                "x",
                "--clinician-name",
                "n",
                "--reg-number",
                "r",
                "--hospital",
                "h",
                "--clinical-email",
                "signatory@org-a.test",
                "--clinical-password-env",
                "MY_CLINICAL_PW",
            ]
        )
        creds = _clinical_credentials(args)
        assert creds == s.ClinicalCredentials(email="signatory@org-a.test", password="s3cret", totp_code=None)

    def test_an_unset_password_variable_is_refused_rather_than_sent_as_empty(self, monkeypatch):
        from review.cli import _clinical_credentials, build_arg_parser

        monkeypatch.delenv("MY_CLINICAL_PW", raising=False)
        args = build_arg_parser().parse_args(
            [
                "approve",
                "--output-dir",
                "x",
                "--clinician-name",
                "n",
                "--reg-number",
                "r",
                "--hospital",
                "h",
                "--clinical-email",
                "signatory@org-a.test",
                "--clinical-password-env",
                "MY_CLINICAL_PW",
            ]
        )
        with pytest.raises(SignoffError, match="MY_CLINICAL_PW"):
            _clinical_credentials(args)

    def test_the_three_identity_flags_are_still_demanded_without_clinical_email(self):
        """
        CONTROL for the join's CLI half: the filesystem-only invocation is
        unchanged -- all three are still required, and the operator still gets
        a usage error rather than a failure from deeper in.
        """
        from review.cli import main as cli_main

        with pytest.raises(SystemExit):
            cli_main(["approve", "--output-dir", "x", "--clinician-name", "Dr. X"])

    def test_they_may_be_omitted_when_clinical_email_is_given(self, tmp_path, monkeypatch):
        """
        With --clinical-email the identity is read from the signing account, so
        retyping it could only produce "identical" or "refused". The CLI stops
        insisting; the failure below is the missing run, not a missing flag.
        """
        from review.cli import main as cli_main

        monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", "password")
        # No geper_results.json: this must reach signoff.py and fail THERE.
        assert (
            cli_main(
                ["approve", "--output-dir", str(tmp_path), "--clinical-email", "signatory@org-a.test", "--reason", "r"]
            )
            == 1
        )

    def test_no_clinical_email_means_no_credentials(self):
        from review.cli import _clinical_credentials, build_arg_parser

        args = build_arg_parser().parse_args(
            ["approve", "--output-dir", "x", "--clinician-name", "n", "--reg-number", "r", "--hospital", "h"]
        )
        assert _clinical_credentials(args) is None
