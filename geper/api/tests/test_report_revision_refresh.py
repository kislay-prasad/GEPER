"""
api/tests/test_report_revision_refresh.py
─────────────────────────────────────────

The authoritative clinical status of a LINKED report reaching the documents GEPER writes,
against a REAL PostgreSQL and the REAL DataAccess (api/tests/conftest.py::clinical_conn --
disposable databases only), the same way test_signoff_clinical_link.py does.

THE GAP THIS CLOSES. A retraction, an amendment or a re-analysis made AFTER sign-off lives only
in the clinical database. The signed artefacts cannot change (the manifest holds their
SHA-256s), so nothing GEPER wrote could ever say so, and the LIMS export would ship a retracted
report with no sign of it. `review/signoff.py::refresh_revision` now writes a separate
CURRENT-STATUS copy (report_current.pdf, report_short_current.pdf, report_current.md,
report_revision_status.json); `approve` / `override` / `withdraw` state the status they can see;
the LIMS export carries it as `report_status`.

THE PROPERTIES, each as a test:
  * the signed originals and the manifest are byte-for-byte unchanged by a refresh;
  * the clinical platform's answer reaches the derived PDFs, Markdown and status JSON;
  * every failure -- authentication, an unreadable or inconsistent revision, a link that
    disagrees with the record, another organisation's report, a tampered signed file -- writes
    NOTHING and is audited, and never produces a document that looks clean;
  * a refresh is all-or-nothing, and a later failure leaves the earlier files as they were.

The question-by-question validation (every refusal path of review/revision_status.py) is in
tests/test_report_revision_status.py against a stand-in platform; here the platform is real.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import uuid

import pytest
from pypdf import PdfReader

from api.tests import test_signoff_clinical_link as _base
from clinical.data_access import AuthenticationError, BrokenRevisionRecordError
from report import export_lims as lims
from review import signoff as s
from review.revision_status import RevisionStatusError, fetch_report_revision_status
from utils.exceptions import LIMSExportBlockedError, SignoffError

# Fixtures bound as names (see clinical/tests/test_w169_portal_visibility.py for why).
dao = _base.dao
clinical = _base.clinical
run_dir = _base.run_dir
linked_run_dir = _base.linked_run_dir
creds = _base.creds

SIGNED_FILES = (
    s.RESULTS_FILENAME,
    s.FULL_PDF_FILENAME,
    s.SHORT_PDF_FILENAME,
    s.MANIFEST_FILENAME,
    s.CLINICAL_LINK_FILENAME,
)
DERIVED_FILES = (
    s.CURRENT_PDF_FILENAME,
    s.CURRENT_SHORT_PDF_FILENAME,
    s.CURRENT_MARKDOWN_FILENAME,
    s.REVISION_STATUS_FILENAME,
)
REASON = "the wrong sample was sequenced"


# ── helpers ──────────────────────────────────────────────────────────────────


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _sha(path):
    return hashlib.sha256(_read(path)).hexdigest()


def _snapshot(directory, names):
    """Bytes of the named files that exist. The audit log is never part of a comparison: a refused or failed
    attempt is APPENDED to it by design, and that is asserted separately."""
    return {
        name: _read(os.path.join(directory, name))
        for name in names
        if name != s.AUDIT_LOG_FILENAME and os.path.exists(os.path.join(directory, name))
    }


def _flat(text):
    """Whitespace-free, so a banner that wraps across lines or PDF text runs still compares."""
    return re.sub(r"\s+", "", text)


def _pdf_text(path):
    return _flat(" ".join(page.extract_text() or "" for page in PdfReader(path).pages))


def _md(directory, name=None):
    with open(os.path.join(directory, name or s.CURRENT_MARKDOWN_FILENAME), encoding="utf-8") as fh:
        return _flat(fh.read())


def _status(directory):
    with open(os.path.join(directory, s.REVISION_STATUS_FILENAME), encoding="utf-8") as fh:
        return json.load(fh)


def _audit(directory):
    path = os.path.join(directory, s.AUDIT_LOG_FILENAME)
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _no_staging_left(directory):
    return glob.glob(os.path.join(directory, ".refresh-*")) == []


def _sign(directory, dao, creds):
    return s.approve(
        directory, clinical_credentials=creds, clinical_reason="independent re-derivation", clinical_data_access=dao
    )


@pytest.fixture
def signed(linked_run_dir, dao, creds):
    """A linked run signed off through the real linked path: clinical report 'approved'."""
    _sign(linked_run_dir, dao, creds)
    return linked_run_dir


def _refresh(directory, dao, creds):
    return s.refresh_revision(directory, clinical_credentials=creds, clinical_data_access=dao)


def _retract(clinical, dao, replacement=None, reason=REASON):
    return dao.retract_report(
        clinical["signatory"], clinical["report_id"], reason=reason, replacement_report_id=replacement
    )


def _draft_report(clinical, dao):
    return dao.create_report(clinical["signatory"], clinical["interpretation_id"])


def _tombstone(conn, report_id, user_id):
    with conn.cursor() as cur:
        cur.execute("UPDATE reports SET tombstoned_at = now(), tombstoned_by = %s WHERE id = %s", (user_id, report_id))
    conn.commit()


def _vcf_id(conn, interpretation_id):
    with conn.cursor() as cur:
        cur.execute("SELECT vcf_id FROM interpretations WHERE id = %s", (interpretation_id,))
        row = cur.fetchone()
    conn.commit()
    return row[0]


def _second_organisation(dao, conn):
    """A second organisation holding a real report, to prove a revision cannot cross organisations."""
    import datetime

    now = datetime.datetime(2026, 9, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
    org_id = dao.create_organisation("Org B")
    admin_id = dao.create_user(org_id, "admin@org-b.test", "password")
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        for role in ("Administrator", "Orderer", "Lab technician"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (admin_id, org_id, role, admin_id, now),
            )
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", now),
        )
    conn.commit()
    admin = dao.login("admin@org-b.test", org_id, "password")
    patient_id = dao.create_patient(admin, "Other Patient", datetime.date(1985, 1, 1), "M")
    consent_id = dao.record_consent(admin, patient_id, "testing")
    order_id = dao.create_order(
        admin,
        patient_id=patient_id,
        test_id=test_id,
        consent_id=consent_id,
        required_scope="testing",
        priority="routine",
    )
    sample_id, run_id, vcf_id, interp_id = (uuid.uuid4() for _ in range(4))
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
            (org_id, vcf_id, run_id, "/tmp/y.vcf", "1" * 64, now, admin.user_id),
        )
        cur.execute(
            "INSERT INTO interpretations (org_id, id, vcf_id, run_document, submission_key, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, interp_id, vcf_id, json.dumps({"variants": []}), "sub-" + str(interp_id), now, admin.user_id),
        )
    conn.commit()
    return {"org_id": org_id, "interpretation_id": interp_id, "report_id": dao.create_report(admin, interp_id)}


def _relink(directory, report_id, interpretation_id):
    """Make the link AND the signed manifest name the same (other) report, so the failure under test is the
    clinical platform's answer and not the earlier manifest-versus-link cross-check."""
    _write_link(directory, report_id=str(report_id), interpretation_id=str(interpretation_id))
    manifest_path = os.path.join(directory, s.MANIFEST_FILENAME)
    manifest = json.loads(_read(manifest_path))
    manifest["clinical"]["report_id"] = str(report_id)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


def _write_link(directory, **fields):
    path = os.path.join(directory, s.CLINICAL_LINK_FILENAME)
    with open(path, encoding="utf-8") as fh:
        link = json.load(fh)
    link.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(link, fh)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The command writes only derived artefacts, and the signed ones never change
# ═════════════════════════════════════════════════════════════════════════════


class TestRefreshWritesOnlyDerivedArtefacts:
    def test_an_unlinked_run_has_nothing_to_refresh(self, run_dir, monkeypatch):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        s.approve(run_dir, "Dr. A. Sharma, MD", "MCI-12345", "ABC Diagnostics")
        before = _snapshot(run_dir, os.listdir(run_dir))
        with pytest.raises(SignoffError, match="not linked to a clinical record"):
            s.refresh_revision(run_dir)
        assert _snapshot(run_dir, os.listdir(run_dir)) == before

    def test_a_linked_run_that_was_never_signed_has_no_signed_report_to_copy(self, linked_run_dir, dao, creds):
        before = _snapshot(linked_run_dir, os.listdir(linked_run_dir))
        with pytest.raises(SignoffError, match="not been signed off"):
            _refresh(linked_run_dir, dao, creds)
        assert _snapshot(linked_run_dir, os.listdir(linked_run_dir)) == before

    def test_the_four_derived_files_appear_and_nothing_else(self, signed, dao, creds):
        before = set(os.listdir(signed))
        result = _refresh(signed, dao, creds)
        assert set(os.listdir(signed)) - before == set(DERIVED_FILES)
        assert result["artefacts"] == sorted(DERIVED_FILES) and result["is_retracted"] is False
        assert _no_staging_left(signed)

    def test_the_signed_originals_and_the_manifest_are_byte_for_byte_unchanged(self, signed, dao, creds):
        before = _snapshot(signed, SIGNED_FILES)
        manifest_before = json.loads(before[s.MANIFEST_FILENAME])
        _refresh(signed, dao, creds)
        _refresh(signed, dao, creds)  # and again: a second refresh must not touch them either
        assert _snapshot(signed, SIGNED_FILES) == before
        # the manifest's recorded hashes still hold for the files on disk
        assert manifest_before["results_json_sha256"] == _sha(os.path.join(signed, s.RESULTS_FILENAME))
        assert manifest_before["pdf_sha256"] == _sha(os.path.join(signed, s.FULL_PDF_FILENAME))
        assert manifest_before["short_pdf_sha256"] == _sha(os.path.join(signed, s.SHORT_PDF_FILENAME))

    def test_current_status_is_not_written_into_the_signed_json(self, signed, clinical, dao, creds):
        _retract(clinical, dao)
        _refresh(signed, dao, creds)
        with open(os.path.join(signed, s.RESULTS_FILENAME), encoding="utf-8") as fh:
            document = json.load(fh)
        for key in ("report_revision", "current_status_copy", "is_retracted"):
            assert key not in document

    def test_every_derived_rendering_says_it_is_a_copy_and_not_a_signoff(self, signed, dao, creds):
        _refresh(signed, dao, creds)
        needle = _flat("CURRENT REPORT STATUS — NOT A NEW CLINICAL SIGN-OFF")
        assert needle in _md(signed)
        assert needle in _pdf_text(os.path.join(signed, s.CURRENT_PDF_FILENAME))
        assert needle in _pdf_text(os.path.join(signed, s.CURRENT_SHORT_PDF_FILENAME))
        # and the signed originals do not carry it
        assert needle not in _pdf_text(os.path.join(signed, s.FULL_PDF_FILENAME))

    def test_the_status_record_is_marked_derived_and_lists_the_hashes_of_what_it_describes(self, signed, dao, creds):
        _refresh(signed, dao, creds)
        status = _status(signed)
        assert status["derived"] is True and status["is_signoff"] is False
        assert status["is_retracted"] is False and status["revision"]["banners"] == []
        for name, digest in status["derived_artefacts_sha256"].items():
            assert _sha(os.path.join(signed, name)) == digest
        manifest = json.loads(_read(os.path.join(signed, s.MANIFEST_FILENAME)))
        assert status["signed_original"]["pdf_sha256"] == manifest["pdf_sha256"]
        assert status["signed_original"]["results_json_sha256"] == manifest["results_json_sha256"]

    def test_an_empty_answer_renders_no_revision_banner(self, signed, dao, creds):
        _refresh(signed, dao, creds)
        assert _flat("THIS REPORT HAS BEEN RETRACTED") not in _md(signed)
        assert _flat("AMENDED REPORT") not in _md(signed)


# ═════════════════════════════════════════════════════════════════════════════
# 2. The clinical platform's answer reaches the derived renderings
# ═════════════════════════════════════════════════════════════════════════════


class TestRetractionReachesTheDerivedArtefacts:
    def test_a_retraction_with_no_replacement(self, signed, clinical, dao, creds):
        _retract(clinical, dao)
        _refresh(signed, dao, creds)
        md = _md(signed)
        assert _flat("*** THIS REPORT HAS BEEN RETRACTED.") in md
        assert _flat("do not act on this document") in md and _flat("No replacement report has been issued") in md
        assert _flat(REASON) in md
        for name in (s.CURRENT_PDF_FILENAME, s.CURRENT_SHORT_PDF_FILENAME):
            text = _pdf_text(os.path.join(signed, name))
            assert _flat("THIS REPORT HAS BEEN RETRACTED") in text and _flat("do not act on this document") in text
        status = _status(signed)
        assert status["is_retracted"] is True and status["revision"]["is_retracted"] is True
        assert status["revision"]["retracted"]["reason"] == REASON
        # the retraction leads: it is above the current-status notice
        assert md.index(_flat("THIS REPORT HAS BEEN RETRACTED")) < md.index(_flat("CURRENT REPORT STATUS"))

    def test_a_retained_replacement_is_named_and_obtainable(self, signed, clinical, dao, creds):
        replacement = _draft_report(clinical, dao)
        _retract(clinical, dao, replacement)
        _refresh(signed, dao, creds)
        md = _md(signed)
        assert _flat(f"obtaining the replacement report ({replacement})") in md
        assert _flat("NO LONGER RETAINED") not in md
        assert _status(signed)["revision"]["retracted"]["replacement_retained"] is True

    def test_a_replacement_that_retention_removed_is_named_as_gone(self, signed, clinical, dao, creds, clinical_conn):
        replacement = _draft_report(clinical, dao)
        _retract(clinical, dao, replacement)
        _tombstone(clinical_conn, replacement, clinical["signatory"].user_id)
        _refresh(signed, dao, creds)
        md = _md(signed)
        assert _flat(f"A replacement report ({replacement}) was issued and is NO LONGER RETAINED") in md
        assert _flat("without first obtaining") not in md
        assert _status(signed)["revision"]["retracted"]["replacement_retained"] is False

    def test_a_status_refresh_after_the_retraction_updates_the_earlier_copy(self, signed, clinical, dao, creds):
        _refresh(signed, dao, creds)
        assert _status(signed)["is_retracted"] is False
        _retract(clinical, dao)
        _refresh(signed, dao, creds)
        assert _status(signed)["is_retracted"] is True
        entries = [e for e in _audit(signed) if e["action"] == "revision_refreshed"]
        assert entries[0]["previous"] is None and entries[0]["current"]["is_retracted"] is False
        assert (
            entries[1]["previous"]["status"]["is_retracted"] is False and entries[1]["current"]["is_retracted"] is True
        )


def _approved_amendment(clinical, dao, reason="updated findings"):
    """An amendment taken all the way to 'approved' -- only an approved amendment supersedes its original."""
    signatory = clinical["signatory"]
    amendment_report_id, _notification = dao.create_amendment(signatory, clinical["report_id"], reason)
    dao.record_sole_signatory_claim(
        signatory,
        interpretation_id=clinical["interpretation_id"],
        report_id=amendment_report_id,
        reason="Sole signatory concurrence.",
    )
    dao.submit_for_review(signatory, amendment_report_id)
    dao.approve_report(signatory, amendment_report_id)
    return amendment_report_id


class TestAmendmentAndReanalysisWordingFromTheRealRecord:
    def test_the_original_of_an_amendment_is_superseded(self, signed, clinical, dao, creds):
        _approved_amendment(clinical, dao)
        _refresh(signed, dao, creds)
        md = _md(signed)
        assert _flat("THIS REPORT HAS BEEN SUPERSEDED") in md and _flat("updated findings") in md
        assert _status(signed)["revision"]["is_superseded"] is True

    def test_the_amendment_itself_says_it_amends(self, signed, clinical, dao, clinical_conn):
        amendment_report_id, _notification = dao.create_amendment(
            clinical["signatory"], clinical["report_id"], "updated findings"
        )
        status = fetch_report_revision_status(
            dao,
            clinical["signatory"],
            org_id=clinical["org_id"],
            interpretation_id=clinical["interpretation_id"],
            report_id=amendment_report_id,
        )
        (banner,) = status.revision["banners"]
        assert banner.startswith("THIS IS AN AMENDED REPORT.") and "updated findings" in banner

    def test_a_retracted_and_superseded_report_states_both(self, signed, clinical, dao, creds):
        _approved_amendment(clinical, dao)
        _retract(clinical, dao)
        _refresh(signed, dao, creds)
        md = _md(signed)
        assert md.index(_flat("THIS REPORT HAS BEEN RETRACTED")) < md.index(_flat("THIS REPORT HAS BEEN SUPERSEDED"))

    def test_a_reanalysis_and_the_report_it_branched_from(self, signed, clinical, dao, creds, clinical_conn):
        child_interp = dao.create_reanalysis(
            clinical["signatory"],
            _vcf_id(clinical_conn, clinical["interpretation_id"]),
            clinical["interpretation_id"],
            {"model_version": "v2"},
        )
        child_report = dao.create_report(clinical["signatory"], child_interp)
        child = fetch_report_revision_status(
            dao,
            clinical["signatory"],
            org_id=clinical["org_id"],
            interpretation_id=child_interp,
            report_id=child_report,
        )
        assert child.revision["is_reanalysis"] is True
        assert "RE-ANALYSIS" in child.revision["banners"][0].upper()
        _refresh(signed, dao, creds)  # the parent report: it has been re-analysed since
        assert _status(signed)["revision"]["has_been_reanalysed"] is True
        assert "it has not been retracted" not in _md(signed)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Failures write nothing and are audited
# ═════════════════════════════════════════════════════════════════════════════


def _assert_refused_untouched(directory, before, exception_type, expect_audit=True):
    """Nothing was published, the signed files and any earlier derived files are as they were."""
    assert _snapshot(directory, SIGNED_FILES + DERIVED_FILES) == before
    assert _no_staging_left(directory)
    if expect_audit:
        last = _audit(directory)[-1]
        assert last["action"] == "revision_refresh_failed" and last["outcome"] == "error"
        assert last["error_type"] == exception_type.__name__
        assert not any(("password" in key or "totp" in key or "session" in key) for key in last)
        assert "not-the-password" not in json.dumps(last)


class TestFailuresNeverProduceMisleadingOutput:
    def _before(self, directory):
        return _snapshot(directory, SIGNED_FILES + DERIVED_FILES)

    def test_wrong_password(self, signed, dao):
        before = self._before(signed)
        bad = s.ClinicalCredentials(email="signatory@org-a.test", password="not-the-password")
        with pytest.raises(AuthenticationError):
            _refresh(signed, dao, bad)
        _assert_refused_untouched(signed, before, AuthenticationError)

    def test_no_credentials(self, signed, dao):
        before = self._before(signed)
        with pytest.raises(SignoffError, match="authenticated person"):
            _refresh(signed, dao, None)
        _assert_refused_untouched(signed, before, SignoffError)

    def test_an_unreadable_revision_record(self, signed, dao, creds, monkeypatch):
        before = self._before(signed)

        def broken(session, report_id):
            raise BrokenRevisionRecordError("a retraction with no usable reason")

        monkeypatch.setattr(dao, "get_report_revision", broken)
        with pytest.raises(RevisionStatusError, match="unreadable|cannot be read"):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, RevisionStatusError)

    def test_a_lookup_that_returns_nothing_is_not_an_empty_status(self, signed, dao, creds, monkeypatch):
        before = self._before(signed)
        monkeypatch.setattr(dao, "get_report_revision", lambda session, report_id: None)
        with pytest.raises(RevisionStatusError, match="not a mapping"):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, RevisionStatusError)

    def test_an_incomplete_retraction_block(self, signed, dao, creds, monkeypatch):
        before = self._before(signed)
        monkeypatch.setattr(
            dao,
            "get_report_revision",
            lambda session, report_id: {"retracted": {"issued_at": "2026-03-01T00:00:00+00:00", "reason": "x"}},
        )
        with pytest.raises(RevisionStatusError):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, RevisionStatusError)

    def test_the_two_reads_of_is_retracted_disagreeing(self, signed, clinical, dao, creds, monkeypatch):
        before = self._before(signed)
        _retract(clinical, dao)
        monkeypatch.setattr(dao, "get_report_revision", lambda session, report_id: {})  # says: not retracted
        with pytest.raises(RevisionStatusError, match="disagree"):
            _refresh(signed, dao, creds)
        assert _snapshot(signed, SIGNED_FILES + DERIVED_FILES) == before  # no 'clean' copy of a retracted report

    @pytest.mark.parametrize("missing", ["org_id", "interpretation_id", "report_id"])
    def test_a_link_missing_an_identifier(self, signed, dao, creds, missing):
        before = self._before(signed)
        _write_link(signed, **{missing: None})
        before[s.CLINICAL_LINK_FILENAME] = _read(os.path.join(signed, s.CLINICAL_LINK_FILENAME))
        with pytest.raises(SignoffError, match=missing):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, SignoffError)

    def test_a_malformed_link_identifier(self, signed, dao, creds):
        _write_link(signed, report_id="not-a-uuid")
        before = self._before(signed)
        with pytest.raises(SignoffError, match="malformed"):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, SignoffError)

    def test_a_link_naming_another_interpretation(self, signed, dao, creds):
        _write_link(signed, interpretation_id=str(uuid.uuid4()))
        before = self._before(signed)
        with pytest.raises(RevisionStatusError, match="disagree about which report"):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, RevisionStatusError)

    def test_a_link_naming_a_report_that_does_not_exist(self, signed, dao, creds):
        _relink(
            signed, uuid.uuid4(), json.loads(_read(os.path.join(signed, s.CLINICAL_LINK_FILENAME)))["interpretation_id"]
        )
        before = self._before(signed)
        with pytest.raises(RevisionStatusError, match="does not exist"):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, RevisionStatusError)

    def test_a_report_of_another_organisation_cannot_be_attached(self, signed, dao, creds, clinical_conn):
        other = _second_organisation(dao, clinical_conn)
        _relink(signed, other["report_id"], other["interpretation_id"])
        before = self._before(signed)
        with pytest.raises(RevisionStatusError, match="does not exist"):
            _refresh(signed, dao, creds)  # organisation A's session cannot even see organisation B's report
        _assert_refused_untouched(signed, before, RevisionStatusError)

    def test_credentials_of_another_organisation_cannot_read_this_one(self, signed, dao, clinical_conn):
        _second_organisation(dao, clinical_conn)
        before = self._before(signed)
        foreign = s.ClinicalCredentials(email="admin@org-b.test", password="password")
        with pytest.raises(AuthenticationError):
            _refresh(signed, dao, foreign)
        _assert_refused_untouched(signed, before, AuthenticationError)

    def test_a_link_naming_another_organisation(self, signed, dao, creds):
        _write_link(signed, org_id=str(uuid.uuid4()))
        before = self._before(signed)
        with pytest.raises(AuthenticationError):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, AuthenticationError)

    def test_a_manifest_that_signed_a_different_report_than_the_link_names(self, signed, dao, creds):
        manifest_path = os.path.join(signed, s.MANIFEST_FILENAME)
        manifest = json.loads(_read(manifest_path))
        manifest["clinical"]["report_id"] = str(uuid.uuid4())
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        before = self._before(signed)
        with pytest.raises(SignoffError, match="disagree about which report"):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, SignoffError)

    @pytest.mark.parametrize("name", [s.RESULTS_FILENAME, s.FULL_PDF_FILENAME, s.SHORT_PDF_FILENAME])
    def test_a_signed_original_that_no_longer_matches_its_signature(self, signed, dao, creds, name):
        with open(os.path.join(signed, name), "ab") as fh:
            fh.write(b" ")
        before = self._before(signed)
        with pytest.raises(SignoffError, match="does not match the hash"):
            _refresh(signed, dao, creds)
        _assert_refused_untouched(signed, before, SignoffError)


class TestARefreshIsAllOrNothing:
    def test_a_rendering_failure_on_the_first_refresh_publishes_nothing(self, signed, dao, creds, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("renderer fell over")

        monkeypatch.setattr(s, "generate_pdf", explode)
        before = _snapshot(signed, SIGNED_FILES)
        with pytest.raises(RuntimeError):
            _refresh(signed, dao, creds)
        assert not any(os.path.exists(os.path.join(signed, name)) for name in DERIVED_FILES)
        assert _snapshot(signed, SIGNED_FILES) == before and _no_staging_left(signed)
        assert _audit(signed)[-1]["action"] == "revision_refresh_failed"

    def test_a_later_failure_leaves_the_earlier_copy_exactly_as_it_was(self, signed, clinical, dao, creds, monkeypatch):
        _refresh(signed, dao, creds)
        first = _snapshot(signed, DERIVED_FILES)
        _retract(clinical, dao)

        def explode(*args, **kwargs):
            raise RuntimeError("renderer fell over")

        monkeypatch.setattr(s, "generate_short_pdf", explode)
        with pytest.raises(RuntimeError):
            _refresh(signed, dao, creds)
        assert _snapshot(signed, DERIVED_FILES) == first  # no half-updated mix of old and new
        assert _status(signed)["is_retracted"] is False and _no_staging_left(signed)


# ═════════════════════════════════════════════════════════════════════════════
# 4. approve / override / withdraw: state the status they can see, refuse if they cannot read it
# ═════════════════════════════════════════════════════════════════════════════


def _reviewer_claims(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reviewer_claims")
        n = cur.fetchone()[0]
    conn.commit()
    return n


def _report_state(conn, report_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
    conn.commit()
    return row[0]


class TestSignOffOperationsReadTheStatusAndFailClosed:
    def _broken(self, dao, monkeypatch):
        def broken(session, report_id):
            raise BrokenRevisionRecordError("unreadable")

        monkeypatch.setattr(dao, "get_report_revision", broken)

    def test_approve_with_an_unreadable_status_changes_nothing_anywhere(
        self, linked_run_dir, clinical, dao, creds, clinical_conn, monkeypatch
    ):
        self._broken(dao, monkeypatch)
        before = _snapshot(linked_run_dir, os.listdir(linked_run_dir))
        with pytest.raises(RevisionStatusError):
            _sign(linked_run_dir, dao, creds)
        assert _snapshot(linked_run_dir, os.listdir(linked_run_dir)) == before
        assert not os.path.exists(os.path.join(linked_run_dir, s.MANIFEST_FILENAME))
        assert _report_state(clinical_conn, clinical["report_id"]) == "draft" and _reviewer_claims(clinical_conn) == 0

    def test_approve_states_a_reanalysis_in_the_signed_pdfs_but_not_in_the_signed_json(
        self, linked_run_dir, clinical, dao, creds, clinical_conn
    ):
        dao.create_reanalysis(
            clinical["signatory"],
            _vcf_id(clinical_conn, clinical["interpretation_id"]),
            clinical["interpretation_id"],
            {"model_version": "v2"},
        )
        manifest = _sign(linked_run_dir, dao, creds)
        assert _flat("reflects the original analysis only") in _pdf_text(
            os.path.join(linked_run_dir, s.FULL_PDF_FILENAME)
        )
        assert _flat("reflects the original analysis only") in _pdf_text(
            os.path.join(linked_run_dir, s.SHORT_PDF_FILENAME)
        )
        with open(os.path.join(linked_run_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
            assert "report_revision" not in json.load(fh)
        assert manifest["pdf_sha256"] == _sha(os.path.join(linked_run_dir, s.FULL_PDF_FILENAME))

    def test_approve_without_a_revision_renders_exactly_what_an_unrevised_approve_always_did(
        self, linked_run_dir, dao, creds
    ):
        _sign(linked_run_dir, dao, creds)
        text = _pdf_text(os.path.join(linked_run_dir, s.FULL_PDF_FILENAME))
        for banner in ("THISREPORTHASBEENRETRACTED", "AMENDEDREPORT", "CURRENTREPORTSTATUS", "RE-ANALYSIS"):
            assert banner not in text.upper()

    def test_override_with_an_unreadable_status_records_no_claim_and_changes_no_file(
        self, linked_run_dir, clinical, dao, creds, clinical_conn, monkeypatch
    ):
        self._broken(dao, monkeypatch)
        document = json.loads(_read(os.path.join(linked_run_dir, s.RESULTS_FILENAME)))
        variant = document["variants"][0]["variant"]
        key = f"{variant['chrom']}:{variant['pos']}:{variant['ref']}>{variant['alt']}"
        before = _snapshot(linked_run_dir, os.listdir(linked_run_dir))
        with pytest.raises(RevisionStatusError):
            s.override(
                linked_run_dir,
                key,
                "Likely Benign",
                "population frequency too high",
                "signatory@org-a.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        assert _snapshot(linked_run_dir, os.listdir(linked_run_dir)) == before
        assert _reviewer_claims(clinical_conn) == 0

    def test_override_on_a_linked_draft_still_records_its_claim_when_the_status_is_readable(
        self, linked_run_dir, clinical, dao, creds, clinical_conn
    ):
        document = json.loads(_read(os.path.join(linked_run_dir, s.RESULTS_FILENAME)))
        variant = document["variants"][0]["variant"]
        key = f"{variant['chrom']}:{variant['pos']}:{variant['ref']}>{variant['alt']}"
        record = s.override(
            linked_run_dir,
            key,
            "Likely Benign",
            "population frequency too high",
            "signatory@org-a.test",
            clinical_credentials=creds,
            clinical_data_access=dao,
        )
        assert record["clinical"]["claim_type"] == "disagree" and _reviewer_claims(clinical_conn) == 1

    def test_withdraw_with_an_unreadable_status_changes_no_file(
        self, run_dir, clinical, dao, creds, clinical_conn, monkeypatch
    ):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        s.approve(run_dir, "Dr. Typed In", "MCI-55555", "Some Lab")
        _write_link_new(run_dir, clinical)
        self._broken(dao, monkeypatch)
        before = _snapshot(run_dir, os.listdir(run_dir))
        with pytest.raises(RevisionStatusError):
            s.withdraw(
                run_dir,
                reason="Signed off in error.",
                actor="x@y.test",
                clinical_credentials=creds,
                clinical_data_access=dao,
            )
        assert _snapshot(run_dir, os.listdir(run_dir)) == before


def _write_link_new(directory, clinical):
    with open(os.path.join(directory, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "org_id": str(clinical["org_id"]),
                "interpretation_id": str(clinical["interpretation_id"]),
                "report_id": str(clinical["report_id"]),
            },
            fh,
        )


# ═════════════════════════════════════════════════════════════════════════════
# 5. LIMS export of a linked run, end to end through the CLI path
# ═════════════════════════════════════════════════════════════════════════════


class TestLimsExportCannotShipARetractedReportWithoutItsStatus:
    def _export(self, directory, dao, creds, monkeypatch, tmp_path, fmt="json"):
        """The production path: lims._main() reading a results file that sits in a linked run directory."""
        # A person's tool: the clinical record arrives over the service's HTTPS API, never a database connection.
        # The seam below hands it the test's own data access in place of the HTTPS client.
        monkeypatch.setattr(s, "_open_clinical_http", lambda url: dao)
        if creds is not None:
            monkeypatch.setenv("GEPER_CLINICAL_PASSWORD", creds.password)
        out = str(tmp_path / f"export.{fmt}")
        argv = ["export_lims", os.path.join(directory, s.RESULTS_FILENAME), f"--{fmt}", out]
        argv += ["--clinical-url", "https://clinical.test"]
        if creds is not None:
            argv += ["--clinical-email", creds.email]
        monkeypatch.setattr("sys.argv", argv)
        lims._main()
        return out

    def test_a_retracted_report_is_exported_with_its_retraction_and_replacement_state(
        self, signed, clinical, dao, creds, monkeypatch, tmp_path, clinical_conn
    ):
        replacement = _draft_report(clinical, dao)
        _retract(clinical, dao, replacement)
        _tombstone(clinical_conn, replacement, clinical["signatory"].user_id)
        out = self._export(signed, dao, creds, monkeypatch, tmp_path)
        data = json.loads(_read(out))
        rs = data["report_status"]
        assert data["export_format_version"] == "1.1.0"
        assert rs["is_retracted"] is True and rs["report_id"] == str(clinical["report_id"])
        assert rs["retraction"]["reason"] == REASON
        assert rs["retraction"]["replacement"] == {"report_id": str(replacement), "retained": False}
        # and the signed JSON the export was read from is untouched
        manifest = json.loads(_read(os.path.join(signed, s.MANIFEST_FILENAME)))
        assert manifest["results_json_sha256"] == _sha(os.path.join(signed, s.RESULTS_FILENAME))

    def test_csv_carries_the_retraction_in_every_row(self, signed, clinical, dao, creds, monkeypatch, tmp_path):
        _retract(clinical, dao)
        out = self._export(signed, dao, creds, monkeypatch, tmp_path, fmt="csv")
        lines = _read(out).decode("utf-8").splitlines()
        assert "report_is_retracted" in lines[0] and all(",True," in line for line in lines[1:] if line)

    def test_an_unretracted_linked_report_exports_under_the_existing_rules_with_an_explicit_status(
        self, signed, dao, creds, monkeypatch, tmp_path
    ):
        out = self._export(signed, dao, creds, monkeypatch, tmp_path)
        rs = json.loads(_read(out))["report_status"]
        assert rs["is_retracted"] is False and rs["retraction"] is None

    def test_no_credentials_means_no_export_and_a_clear_audit_entry(self, signed, clinical, dao, monkeypatch, tmp_path):
        _retract(clinical, dao)
        with pytest.raises(LIMSExportBlockedError, match="never exported without its status"):
            self._export(signed, dao, None, monkeypatch, tmp_path)
        assert not (tmp_path / "export.json").exists()
        assert _audit(signed)[-1]["action"] == "export_blocked"

    def test_wrong_credentials_mean_no_export(self, signed, clinical, dao, monkeypatch, tmp_path):
        _retract(clinical, dao)
        bad = s.ClinicalCredentials(email="signatory@org-a.test", password="nope")
        with pytest.raises(LIMSExportBlockedError):
            self._export(signed, dao, bad, monkeypatch, tmp_path)
        assert not (tmp_path / "export.json").exists()

    def test_an_unreadable_revision_means_no_export(self, signed, clinical, dao, creds, monkeypatch, tmp_path):
        _retract(clinical, dao)

        def broken(session, report_id):
            raise BrokenRevisionRecordError("unreadable")

        monkeypatch.setattr(dao, "get_report_revision", broken)
        with pytest.raises(LIMSExportBlockedError):
            self._export(signed, dao, creds, monkeypatch, tmp_path)
        assert not (tmp_path / "export.json").exists()

    def test_an_unsigned_linked_run_is_still_refused_for_review(
        self, linked_run_dir, dao, creds, monkeypatch, tmp_path
    ):
        with pytest.raises(LIMSExportBlockedError, match="review_status"):
            self._export(linked_run_dir, dao, creds, monkeypatch, tmp_path)

    def test_an_unlinked_run_exports_exactly_as_before(self, run_dir, monkeypatch, tmp_path):
        monkeypatch.delenv("CLINICAL_DSN", raising=False)
        s.approve(run_dir, "Dr. A. Sharma, MD", "MCI-12345", "ABC Diagnostics")
        out = str(tmp_path / "export.json")
        monkeypatch.setattr("sys.argv", ["export_lims", os.path.join(run_dir, s.RESULTS_FILENAME), "--json", out])
        lims._main()
        data = json.loads(_read(out))
        assert "report_status" not in data and data["export_format_version"] == "1.0.0"
