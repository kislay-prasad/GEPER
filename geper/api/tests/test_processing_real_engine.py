"""
api/tests/test_processing_real_engine.py
────────────────────────────────────────
OPT-IN. The worker driving the REAL engine (`geper/main.py`) for a QC-passed clinical sample, into a draft report, with a
real PostgreSQL. Marked `live_network`, which `pytest.ini` excludes from every bare `pytest` run: the engine makes live
calls to third-party services (Ensembl, NCBI BLAST, ClinVar and others), downloads model weights on first use, and can
take many minutes. Run it deliberately:

    pytest -m live_network api/tests/test_processing_real_engine.py

(with the disposable-database variables `api/tests/conftest.py` documents, and Python 3.11 to 3.12).

WHAT THIS IS, AND IS NOT. It shows the hand-off works with the actual engine: the engine accepts the worker's command
line, finishes, writes a document the worker's output validation accepts, and that document becomes a draft report
attached to the right patient, order and sample. It runs ONE synthetic variant. It says nothing about the accuracy of the
interpretation, its biological or clinical validity, or any other input. FASTQ -> VCF is not covered: it needs
`kim_pipeline` and an aligner and a variant caller (bwa or minimap2, freebayes, bcftools, samtools), and this test starts
from a VCF.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from api import submission_worker
from api.tests.conftest import place_vcf
from api.submission_worker import InterpretationWorker, validate_engine_output
from api.tests import test_processing_handoff as _h

# fixtures and helpers shared with the stand-in tests
api = _h.api
dao = _h.dao
store = _h.store
store_path = _h.store_path
output_root = _h.output_root
KEY_A = _h.KEY_A
NOTHING_RECORDED = _h.NOTHING_RECORDED
clinical_counts = _h.clinical_counts
make_org = _h.make_org
process = _h.process
q = _h.q
submit = _h.submit

GEPER_DIR = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.live_network,
    pytest.mark.skipif(
        not ((3, 11) <= sys.version_info[:2] < (3, 13)),
        reason="geper/main.py refuses to start outside Python 3.11 to 3.12",
    ),
]

# One synthetic variant on GRCh38; the reference base at 17:43093449 is G.
VCF = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "##contig=<ID=17,assembly=GRCh38>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSYNTH01\n"
    "17\t43093449\t.\tG\tA\t60\tPASS\t.\tGT\t0/1\n"
)


def test_the_real_engine_turns_a_qc_passed_sample_into_a_draft_report(
    api, store, dao, clinical_conn, output_root, vcf_root, monkeypatch
):
    monkeypatch.delenv("BIJ_AI_CLI_PATH", raising=False)  # the actual geper/main.py, not a stand-in
    monkeypatch.setattr(submission_worker, "INTERPRETATION_TIMEOUT_SECONDS", 1700)
    worker = InterpretationWorker(store, dao)
    assert worker.geper_main_path == str(GEPER_DIR / "main.py")

    org = make_org(dao, clinical_conn, "Org Real Engine", qc="passed")
    vcf = Path(place_vcf(vcf_root, org["org_id"], VCF, "synthetic.vcf"))
    sub_id = submit(api(org["org_id"], KEY_A), org, vcf)

    done = process(worker, store, sub_id)
    assert done.status == "complete", done.error_message

    ((document, report_state, sample_id, order_id),) = [
        (row[0], row[1], row[2], row[3])
        for row in q(
            clinical_conn,
            "SELECT i.run_document, r.state, s.sample_id, o.order_id FROM reports r "
            "JOIN interpretations i ON i.org_id = r.org_id AND i.id = r.interpretation_id "
            "JOIN vcfs v ON v.org_id = i.org_id AND v.id = i.vcf_id "
            "JOIN sequencing_runs sr ON sr.org_id = v.org_id AND sr.id = v.sequencing_run_id "
            "JOIN samples s ON s.org_id = sr.org_id AND s.sample_id = sr.sample_id "
            "JOIN orders o ON o.org_id = s.org_id AND o.order_id = s.order_id "
            "WHERE r.org_id = %s",
            (org["org_id"],),
        )
    ]
    document = json.loads(document) if isinstance(document, str) else document
    assert report_state == "draft" and (sample_id, order_id) == (org["sample_id"], org["order_id"])
    assert validate_engine_output(document, str(vcf)) == []
    assert document["run_complete"] is True and document["variant_count"] == 1
    assert document["review_status"] == "draft" and document["reviewed_by"] is None
    assert document.get("geper_version") or document.get("code_version")  # the run records which code produced it
    assert clinical_counts(clinical_conn, org["org_id"]) == {k: 1 for k in NOTHING_RECORDED}
    attempt = json.loads((output_root / sub_id / "attempt.json").read_text(encoding="utf-8"))
    assert attempt["attempt"] == 1 and attempt["submission_id"] == sub_id
