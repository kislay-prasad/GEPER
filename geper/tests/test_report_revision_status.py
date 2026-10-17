"""
tests/test_report_revision_status.py
────────────────────────────────────

The VALIDATION of a linked report's clinical status (review/revision_status.py), the
CURRENT-STATUS COPY notice, and the LIMS export's handling of that status
(report/export_lims.py). No database: the clinical platform is a stand-in that returns
exactly the shapes `DataAccess.get_report` / `get_report_revision` return, so every
refusal path is reachable on demand. The real DataAccess + PostgreSQL versions of the same
questions are in api/tests/test_report_revision_refresh.py.

WHAT THESE PIN
  * A failed, partial, mismatched or inconsistent lookup is a `RevisionStatusError` and never
    an empty / unflagged status.
  * `{}` is a successful "nothing to say", and renders no revision banner.
  * Each supported retraction form, the amendment and the re-analysis wording.
  * The LIMS export: unchanged for an unlinked run; for a linked run, the status travels in
    `report_status` (and in extra CSV columns); without it the export is blocked.
"""

from __future__ import annotations

import csv
import json
import os
import uuid

import pytest

from clinical.data_access import (
    AuthorizationError,
    BrokenRevisionRecordError,
    NotFoundError,
    SessionExpired,
)
from report import export_lims as lims
from report.clinical_report_builder import (
    CURRENT_STATUS_COPY_BANNER,
    REPORT_REVISION_RETRACTED_BANNER,
    REPORT_REVISION_RETRACTED_REPLACED_BANNER,
    REPORT_REVISION_RETRACTED_REPLACEMENT_NOT_RETAINED_BANNER,
    apply_report_revision,
    report_revision_banners,
)
from review.revision_status import RevisionStatusError, fetch_report_revision_status
from tests.test_signoff import _make_document
from utils.exceptions import LIMSExportBlockedError, SignoffError

ORG = uuid.uuid4()
INTERP = uuid.uuid4()
REPORT = uuid.uuid4()
REPLACEMENT = uuid.uuid4()

RETRACTED = {
    "issued_at": "2026-03-01T09:00:00+00:00",
    "issued_basis": "released",
    "retracted_at": "2026-04-05T10:00:00+00:00",
    "retracted_by": "Dr. Example, MD",
    "reason": "wrong sample was sequenced",
}


class _Session:
    def __init__(self, org_id=ORG):
        self.org_id = org_id
        self.user_id = uuid.uuid4()


class _Platform:
    """The two DataAccess reads, returning what the real ones return."""

    def __init__(self, revision=None, report="default", revision_exc=None, report_exc=None):
        self.revision = {} if revision is None else revision
        self.report = (
            {
                "id": REPORT,
                "interpretation_id": INTERP,
                "state": "approved",
                "is_retracted": "retracted" in self.revision,
            }
            if report == "default"
            else report
        )
        self.revision_exc = revision_exc
        self.report_exc = report_exc

    def get_report(self, session, report_id):
        if self.report_exc:
            raise self.report_exc
        return self.report

    def get_report_revision(self, session, report_id):
        if self.revision_exc:
            raise self.revision_exc
        return self.revision


def _fetch(platform, session=None, **overrides):
    ids = dict(org_id=ORG, interpretation_id=INTERP, report_id=REPORT)
    ids.update(overrides)
    return fetch_report_revision_status(platform, session or _Session(), **ids)


class TestAnEmptyAnswerIsASuccessNotAnAbsence:
    def test_an_empty_mapping_is_a_validated_all_false_status(self):
        status = _fetch(_Platform({}))
        assert status.is_retracted is False
        assert status.revision["banners"] == []
        assert status.revision["is_amendment"] is False and status.revision["is_superseded"] is False
        assert status.revision is not None  # never None: "looked and found nothing", not "never looked"

    def test_the_status_names_what_it_was_read_for(self):
        status = _fetch(_Platform({}))
        assert (status.org_id, status.interpretation_id, status.report_id) == (str(ORG), str(INTERP), str(REPORT))
        assert status.report_state == "approved" and status.checked_at


class TestRetractionForms:
    def test_no_replacement(self):
        status = _fetch(_Platform({"retracted": dict(RETRACTED)}))
        assert status.is_retracted is True
        (banner,) = status.revision["banners"]
        assert banner.startswith("*** THIS REPORT HAS BEEN RETRACTED.")
        assert "do not act on this document" in banner and "No replacement report has been issued" in banner
        assert "wrong sample was sequenced" in banner
        assert status.retraction["reason"] == "wrong sample was sequenced"

    def test_a_retained_replacement_is_named_and_obtainable(self):
        block = dict(RETRACTED, replacement_report_id=str(REPLACEMENT), replacement_retained=True)
        (banner,) = _fetch(_Platform({"retracted": block})).revision["banners"]
        assert str(REPLACEMENT) in banner
        assert "without first obtaining the replacement report" in banner
        assert "NO LONGER RETAINED" not in banner

    def test_a_replacement_that_retention_removed_is_named_as_gone(self):
        block = dict(RETRACTED, replacement_report_id=str(REPLACEMENT), replacement_retained=False)
        (banner,) = _fetch(_Platform({"retracted": block})).revision["banners"]
        assert str(REPLACEMENT) in banner and "NO LONGER RETAINED" in banner and "cannot be obtained" in banner
        assert "without first obtaining" not in banner

    def test_the_three_forms_use_the_three_signed_off_templates(self):
        assert "{id}" not in REPORT_REVISION_RETRACTED_BANNER
        assert "{id}" in REPORT_REVISION_RETRACTED_REPLACED_BANNER
        assert "NO LONGER RETAINED" in REPORT_REVISION_RETRACTED_REPLACEMENT_NOT_RETAINED_BANNER


class TestAmendmentAndReanalysisWordingIsUnchanged:
    AMENDS = {
        "original_report_id": str(uuid.uuid4()),
        "original_issued_at": "2026-02-01T09:00:00+00:00",
        "reason": "updated findings",
        "amended_by": "Dr. Other",
        "amended_at": "2026-02-10T09:00:00+00:00",
    }

    def test_an_amendment(self):
        (banner,) = _fetch(_Platform({"amends": dict(self.AMENDS)})).revision["banners"]
        assert banner.startswith("THIS IS AN AMENDED REPORT.") and "updated findings" in banner

    def test_a_reanalysis_and_a_report_that_has_been_reanalysed(self):
        raw = {
            "reanalysis_of": {
                "parent_interpretation_id": str(uuid.uuid4()),
                "parent_interpreted_at": "2026-01-01T00:00:00+00:00",
            },
            "reanalysed_since": [{"interpretation_id": str(uuid.uuid4()), "created_at": "2026-05-01T00:00:00+00:00"}],
        }
        banners = _fetch(_Platform(raw)).revision["banners"]
        assert len(banners) == 2
        assert "RE-ANALYSIS" in banners[0].upper() or "re-analysis" in banners[0].lower()
        # the clause that used to claim "it has not been retracted" must not
        assert all("it has not been retracted" not in b for b in banners)

    def test_retracted_and_amended_says_both_retraction_first(self):
        status = _fetch(_Platform({"retracted": dict(RETRACTED), "amends": dict(self.AMENDS)}))
        first, second = status.revision["banners"]
        assert first.startswith("*** THIS REPORT HAS BEEN RETRACTED.") and second.startswith(
            "THIS IS AN AMENDED REPORT."
        )


class TestEveryFailureIsAnErrorNeverAnUnflaggedStatus:
    @pytest.mark.parametrize(
        "exc",
        [
            BrokenRevisionRecordError("a retraction with no usable reason"),
            NotFoundError("gone"),
            AuthorizationError("no"),
            SessionExpired(),
            RuntimeError("database fell over"),
        ],
    )
    def test_a_failed_revision_lookup(self, exc):
        with pytest.raises(RevisionStatusError):
            _fetch(_Platform({}, revision_exc=exc))

    @pytest.mark.parametrize("exc", [AuthorizationError("no"), SessionExpired(), RuntimeError("down")])
    def test_a_failed_report_lookup(self, exc):
        with pytest.raises(RevisionStatusError):
            _fetch(_Platform({}, report_exc=exc))

    @pytest.mark.parametrize("answer", [None, [], "", "{}", 0])
    def test_a_revision_that_is_not_a_mapping_is_not_an_empty_one(self, answer):
        platform = _Platform({})
        platform.revision = answer
        with pytest.raises(RevisionStatusError):
            _fetch(platform)

    def test_a_malformed_retraction_block_is_refused(self):
        for broken in (
            {k: v for k, v in RETRACTED.items() if k != "reason"},
            dict(RETRACTED, reason="   "),
            dict(RETRACTED, replacement_report_id=str(REPLACEMENT)),  # id without its retention state
            dict(RETRACTED, replacement_retained=True),  # retention state without an id
            dict(RETRACTED, issued_basis="sometime"),
        ):
            platform = _Platform({"retracted": broken})
            platform.report = {"id": REPORT, "interpretation_id": INTERP, "state": "approved", "is_retracted": True}
            with pytest.raises(RevisionStatusError):
                _fetch(platform)

    def test_the_two_reads_of_is_retracted_must_agree(self):
        platform = _Platform({"retracted": dict(RETRACTED)})
        platform.report["is_retracted"] = False
        with pytest.raises(RevisionStatusError, match="disagree"):
            _fetch(platform)
        clean = _Platform({})
        clean.report["is_retracted"] = True
        with pytest.raises(RevisionStatusError, match="disagree"):
            _fetch(clean)


class TestARevisionCannotCrossABoundary:
    def test_a_session_from_another_organisation_is_refused(self):
        with pytest.raises(RevisionStatusError, match="organisation"):
            _fetch(_Platform({}), session=_Session(org_id=uuid.uuid4()))

    def test_a_report_the_platform_cannot_find_in_the_organisation_is_refused(self):
        with pytest.raises(RevisionStatusError, match="does not exist"):
            _fetch(_Platform({}, report=None))

    def test_a_report_of_another_interpretation_is_refused(self):
        other = {"id": REPORT, "interpretation_id": uuid.uuid4(), "state": "approved", "is_retracted": False}
        with pytest.raises(RevisionStatusError, match="disagree about which report"):
            _fetch(_Platform({}, report=other))

    def test_a_link_naming_another_interpretation_is_refused(self):
        with pytest.raises(RevisionStatusError, match="disagree about which report"):
            _fetch(_Platform({}), interpretation_id=uuid.uuid4())


class TestTheCurrentStatusCopyIsNeverMistakenForTheSignedReport:
    COPY = {"signed_at": "2026-03-01T09:00:00+00:00", "checked_at": "2026-04-06T09:00:00+00:00"}

    def test_the_notice_follows_the_revision_banners_so_a_retraction_still_leads(self):
        document = apply_report_revision({}, _fetch(_Platform({"retracted": dict(RETRACTED)})).revision)
        document["current_status_copy"] = dict(self.COPY)
        banners = report_revision_banners(document)
        assert banners[0].startswith("*** THIS REPORT HAS BEEN RETRACTED.")
        assert banners[-1].startswith("CURRENT REPORT STATUS — NOT A NEW CLINICAL SIGN-OFF.")
        assert "signed original remains the clinical record" in banners[-1]
        assert "has not been reviewed or signed by a clinician" in banners[-1]
        assert "refers to the signed original" in banners[-1]

    def test_an_unrevised_copy_still_says_it_is_a_copy(self):
        document = apply_report_revision({}, _fetch(_Platform({})).revision)
        document["current_status_copy"] = dict(self.COPY)
        (banner,) = report_revision_banners(document)
        assert "CURRENT REPORT STATUS" in banner and "{signed_at}" not in banner

    def test_a_copy_that_cannot_say_what_it_is_fails_closed(self):
        for meta in ({}, {"signed_at": "x"}, {"checked_at": "y"}, "nope"):
            with pytest.raises(ValueError):
                report_revision_banners({"report_revision": None, "current_status_copy": meta})

    def test_a_document_without_the_marker_is_unchanged(self):
        assert report_revision_banners({"report_revision": None}) == []
        assert "{signed_at}" in CURRENT_STATUS_COPY_BANNER


# ── LIMS export ──────────────────────────────────────────────────────────────


def _reviewed_document():
    document = _make_document()
    document["review_status"] = "reviewed"
    return document


def _link_dir(tmp_path, report_id=REPORT, org_id=ORG):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "clinical_link.json").write_text(
        json.dumps({"org_id": str(org_id), "interpretation_id": str(INTERP), "report_id": str(report_id)}),
        encoding="utf-8",
    )
    return str(run_dir)


def _audit_actions(directory):
    path = os.path.join(directory, "geper_signoff_audit.log")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestLimsExportOfAnUnlinkedRunIsUnchanged:
    def test_json_has_no_report_status_and_the_original_version(self, tmp_path):
        out = tmp_path / "x.json"
        lims.export_lims_json(_reviewed_document(), str(out), run_dir=str(tmp_path))
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "report_status" not in data and data["export_format_version"] == "1.0.0"

    def test_csv_has_exactly_the_original_columns(self, tmp_path):
        out = tmp_path / "x.csv"
        lims.export_lims_csv(_reviewed_document(), str(out), run_dir=str(tmp_path))
        with open(out, encoding="utf-8", newline="") as fh:
            assert csv.DictReader(fh).fieldnames == lims._CSV_COLUMNS

    def test_the_review_requirement_is_still_enforced(self, tmp_path):
        document = _make_document()  # a draft
        with pytest.raises(LIMSExportBlockedError, match="review_status"):
            lims.export_lims_json(document, str(tmp_path / "x.json"), run_dir=str(tmp_path))
        assert not (tmp_path / "x.json").exists()


class TestLimsExportOfALinkedRunCarriesItsStatus:
    def _status(self, revision):
        return _fetch(_Platform(revision))

    def test_a_retracted_report_is_exported_with_its_retraction(self, tmp_path):
        run_dir = _link_dir(tmp_path)
        status = self._status(
            {"retracted": dict(RETRACTED, replacement_report_id=str(REPLACEMENT), replacement_retained=True)}
        )
        out = tmp_path / "x.json"
        lims.export_lims_json(_reviewed_document(), str(out), run_dir=run_dir, report_status=status)
        data = json.loads(out.read_text(encoding="utf-8"))
        rs = data["report_status"]
        assert data["export_format_version"] == "1.1.0"
        assert rs["is_retracted"] is True and rs["report_id"] == str(REPORT) and rs["source"] == "clinical_platform"
        assert rs["retraction"]["reason"] == "wrong sample was sequenced"
        assert rs["retraction"]["replacement"] == {"report_id": str(REPLACEMENT), "retained": True}
        assert rs["banners"] and rs["banners"][0].startswith("*** THIS REPORT HAS BEEN RETRACTED.")

    def test_a_replacement_no_longer_retained_is_stated_explicitly(self, tmp_path):
        status = self._status(
            {"retracted": dict(RETRACTED, replacement_report_id=str(REPLACEMENT), replacement_retained=False)}
        )
        out = tmp_path / "x.json"
        lims.export_lims_json(_reviewed_document(), str(out), run_dir=_link_dir(tmp_path), report_status=status)
        assert json.loads(out.read_text())["report_status"]["retraction"]["replacement"]["retained"] is False

    def test_a_retracted_report_without_a_replacement(self, tmp_path):
        out = tmp_path / "x.json"
        lims.export_lims_json(
            _reviewed_document(),
            str(out),
            run_dir=_link_dir(tmp_path),
            report_status=self._status({"retracted": dict(RETRACTED)}),
        )
        assert json.loads(out.read_text())["report_status"]["retraction"]["replacement"] is None

    def test_an_unrevised_linked_report_exports_with_an_explicit_all_false_status(self, tmp_path):
        out = tmp_path / "x.json"
        lims.export_lims_json(
            _reviewed_document(), str(out), run_dir=_link_dir(tmp_path), report_status=self._status({})
        )
        rs = json.loads(out.read_text())["report_status"]
        assert rs["is_retracted"] is False and rs["retraction"] is None and rs["banners"] == []

    def test_csv_gains_the_status_columns_and_keeps_the_original_ones(self, tmp_path):
        out = tmp_path / "x.csv"
        status = self._status(
            {"retracted": dict(RETRACTED, replacement_report_id=str(REPLACEMENT), replacement_retained=False)}
        )
        lims.export_lims_csv(_reviewed_document(), str(out), run_dir=_link_dir(tmp_path), report_status=status)
        with open(out, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            assert reader.fieldnames[: len(lims._CSV_COLUMNS)] == lims._CSV_COLUMNS
            assert reader.fieldnames[len(lims._CSV_COLUMNS) :] == lims._CSV_STATUS_COLUMNS
        assert rows and all(
            r["report_is_retracted"] == "True" and r["report_replacement_retained"] == "False" for r in rows
        )
        assert rows[0]["report_replacement_report_id"] == str(REPLACEMENT)


class TestLimsExportOfALinkedRunFailsClosed:
    def test_no_status_means_no_export_and_an_audit_entry(self, tmp_path):
        run_dir = _link_dir(tmp_path)
        out = os.path.join(run_dir, "x.json")
        with pytest.raises(LIMSExportBlockedError, match="linked to a clinical record"):
            lims.export_lims_json(_reviewed_document(), out, run_dir=run_dir)
        assert not os.path.exists(out)
        assert _audit_actions(run_dir)[-1]["action"] == "export_blocked"

    def test_csv_too(self, tmp_path):
        run_dir = _link_dir(tmp_path)
        out = os.path.join(run_dir, "x.csv")
        with pytest.raises(LIMSExportBlockedError):
            lims.export_lims_csv(_reviewed_document(), out, run_dir=run_dir)
        assert not os.path.exists(out)

    def test_a_status_for_another_report_or_organisation_is_refused(self, tmp_path):
        status = _fetch(_Platform({}))
        for run_dir in (_link_dir(tmp_path, report_id=uuid.uuid4()), _link_dir(tmp_path, org_id=uuid.uuid4())):
            with pytest.raises(LIMSExportBlockedError, match="different report or organisation"):
                lims.export_lims_json(
                    _reviewed_document(), os.path.join(run_dir, "x.json"), run_dir=run_dir, report_status=status
                )

    def test_an_unreadable_link_is_a_block_not_an_unlinked_export(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "clinical_link.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(LIMSExportBlockedError, match="cannot be read"):
            lims.export_lims_json(_reviewed_document(), str(run_dir / "x.json"), run_dir=str(run_dir))

    def test_an_unreviewed_linked_run_is_still_blocked_for_review_first(self, tmp_path):
        run_dir = _link_dir(tmp_path)
        with pytest.raises(LIMSExportBlockedError, match="review_status"):
            lims.export_lims_json(
                _make_document(), os.path.join(run_dir, "x.json"), run_dir=run_dir, report_status=_fetch(_Platform({}))
            )

    def test_the_cli_path_blocks_and_audits_when_the_status_cannot_be_read(self, tmp_path, monkeypatch):
        run_dir = _link_dir(tmp_path)

        def _boom(link, credentials, data_access=None):
            raise SignoffError("no credentials were supplied")

        monkeypatch.setattr("review.signoff.fetch_linked_revision_status", _boom)
        with pytest.raises(LIMSExportBlockedError, match="never exported without its status"):
            lims._status_for_cli_export(run_dir, object(), lambda a: None, _reviewed_document())
        entry = _audit_actions(run_dir)[-1]
        assert entry["action"] == "export_blocked" and "no credentials" in entry["reason"]

    def test_the_cli_path_returns_none_for_an_unlinked_run(self, tmp_path):
        assert lims._status_for_cli_export(str(tmp_path), object(), lambda a: None, _reviewed_document()) is None
