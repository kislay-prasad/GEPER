"""
The pure-module side of call-set storage: the compact derived document, its rehydration through the validating models,
the integrity re-parse, the record cap and the parser rule-set registry. No database.

Each test names the thing it would catch and has a positive twin where an absence is asserted.
"""

from __future__ import annotations

import json

import pytest

from clinical.genome_analysis import (
    CALLSET_DOCUMENT_SCHEMA_VERSION,
    CALLSET_PARSER_VERSION,
    PARSER_RULESETS,
    CallSetRejected,
    StructuralValidationError,
    callset_from_document,
    parse_structural_vcf,
    verify_document,
    verify_document_for_version,
)

HEAD = "##fileformat=VCFv4.2\n##contig=<ID=1,length=248956422>\n##contig=<ID=2,length=242193529>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n"
SV = (
    "1\t1000\tdel1\tN\t<DEL>\t50\tPASS\tSVTYPE=DEL;END=5000;CIPOS=-5,5;SECRET=evidence-marker\tGT\t0/1\n"
    "1\t9000\tb1\tG\tG]2:700]\t.\tPASS\tSVTYPE=BND;MATEID=b2\tGT\t0/1\n"
    "1\t20000\tinv1\tN\t<INV>\t.\tLowQual\tSVTYPE=INV;END=30000;IMPRECISE\tGT\t0/1\n"
    "1\tnot-a-number\tbad\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END=40000\tGT\t0/1\n"
)
CNV = (
    "1\t1000\tc1\tN\t<DUP>\t1.5e22\tPASS\tSVTYPE=DUP;END=5000;CN=3;CIPOS=-10,10;CIEND=-5,5\tGT:CN\t0/1:3\n"
    "1\t9000\tc2\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END=12000;CN=0\tGT:CN\t0/1:0\n"
)


def parse(body, record_class, **kw):
    return parse_structural_vcf(HEAD + body, assembly="GRCh38", record_class=record_class, **kw)


class TestCompactDocument:
    def test_one_entry_per_input_record_in_order_with_status_reason_and_line_number(self):
        doc = parse(SV, "sv").to_document()
        assert doc["schema_version"] == CALLSET_DOCUMENT_SCHEMA_VERSION
        assert [(e["line_no"], e["status"]) for e in doc["records"]] == [
            (5, "detected"),
            (6, "detected"),
            (7, "uncertain"),
            (8, "invalid"),
        ]
        assert doc["records"][3]["record"] is None and doc["records"][3]["reason"], "an invalid record keeps WHY"
        assert doc["counts"] == {"detected": 2, "uncertain": 1, "unsupported": 0, "invalid": 1, "records": 4}
        assert doc["samples"] == ["S1"] and doc["assembly"] == "GRCh38" and doc["record_class"] == "sv"

    def test_the_per_record_evidence_object_is_not_stored_and_the_document_is_json_safe(self):
        full = parse(SV, "sv")
        assert any(r.evidence for r in full.svs), "twin: the full model DOES carry evidence"
        text = json.dumps(full.to_document())
        assert "evidence-marker" not in text and '"evidence"' not in text
        assert json.loads(text) == full.to_document()

    def test_cnv_values_including_an_extreme_quality_survive_a_json_round_trip_exactly(self):
        doc = json.loads(json.dumps(parse(CNV, "cnv").to_document()))
        first = doc["records"][0]["record"]
        assert first["quality"] == 1.5e22 and first["copy_number"] == 3.0
        assert doc["records"][1]["record"]["copy_number"] == 0.0


class TestRehydration:
    def test_a_document_rebuilds_through_the_validating_models(self):
        original = parse(SV + "", "sv")
        back = callset_from_document(
            json.loads(json.dumps(original.to_document())), caller="Manta 1.6", call_set_id="cs-1"
        )
        assert back.counts() == original.counts()
        assert [(s.sv_type, s.status, [(b.chrom, b.pos) for b in s.breakpoints]) for s in back.svs] == [
            (s.sv_type, s.status, [(b.chrom, b.pos) for b in s.breakpoints]) for s in original.svs
        ]
        assert all(s.caller == "Manta 1.6" and s.call_set_id == "cs-1" for s in back.svs)
        assert [o.reason for o in back.rejected] == [o.reason for o in original.rejected]
        assert all(s.evidence == {} for s in back.svs), "evidence is not stored, so it is empty after rehydration"

    def test_cnv_rehydrates_with_its_copy_number_and_breakpoint_intervals(self):
        back = callset_from_document(json.loads(json.dumps(parse(CNV, "cnv").to_document())))
        first = back.cnvs[0]
        assert (first.start, first.end, first.copy_number, first.quality) == (1000, 5000, 3.0, 1.5e22)
        assert first.start_breakpoint.ci_low == 990 and first.end_breakpoint.ci_high == 5005

    @pytest.mark.parametrize(
        "corrupt",
        [
            lambda d: d["records"][0]["record"]["breakpoints"][0].update(pos=999999999999),
            lambda d: d["records"][0]["record"].update(sv_type="NOT_A_TYPE"),
            lambda d: d["records"][0]["record"]["breakpoints"][0].update(ci_low=10**9),
            lambda d: d["records"][0]["record"].update(status="surely"),
        ],
        ids=["position-off-chromosome", "unknown-type", "interval-excludes-position", "unknown-status"],
    )
    def test_a_document_that_no_longer_satisfies_the_models_is_refused_not_drawn(self, corrupt):
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        callset_from_document(doc)  # positive twin: the uncorrupted document rebuilds
        corrupt(doc)
        with pytest.raises(StructuralValidationError):
            callset_from_document(doc)

    def test_an_unknown_document_schema_version_is_refused(self):
        doc = parse(SV, "sv").to_document()
        doc["schema_version"] = "99"
        with pytest.raises(CallSetRejected, match="schema version"):
            callset_from_document(doc)


class TestIntegrityReparse:
    def test_the_stored_document_verifies_against_exactly_its_own_bytes(self):
        text = HEAD + SV
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        assert verify_document(text, doc) is True
        assert verify_document(text.encode("utf-8"), doc) is True

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda t: t.replace("END=5000", "END=5001"),  # a coordinate
            lambda t: t.replace("\t50\t", "\t51\t"),  # a quality
            lambda t: t + "1\t50000\textra\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END=60000\tGT\t0/1\n",  # an added record
            lambda t: t.replace("LowQual", "PASS"),  # a FILTER change (detected vs uncertain)
        ],
        ids=["coordinate", "quality", "added-record", "filter"],
    )
    def test_any_change_to_the_bytes_is_detected(self, mutate):
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        assert verify_document(mutate(HEAD + SV), doc) is False

    def test_a_document_edited_after_the_fact_is_detected(self):
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        doc["records"][0]["status"] = "uncertain"
        assert verify_document(HEAD + SV, doc) is False

    def test_a_document_whose_bytes_are_not_a_vcf_or_whose_header_is_missing_is_false_not_an_error(self):
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        assert verify_document("garbage", doc) is False
        assert verify_document(HEAD + SV, {"nothing": 1}) is False


class TestRecordCap:
    def test_the_cap_is_enforced_before_any_record_is_parsed(self, monkeypatch):
        import clinical.genome_analysis.structural_vcf as sv

        def must_not_run(*args, **kwargs):
            raise AssertionError("a record was parsed although the cap was already exceeded")

        monkeypatch.setattr(sv, "_parse_sv", must_not_run)
        with pytest.raises(CallSetRejected, match="4 records.*limit is 3"):
            parse(SV, "sv", max_records=3)

    def test_at_the_cap_the_file_is_accepted_and_none_means_unlimited(self):
        assert parse(SV, "sv", max_records=4).counts()["records"] == 4
        assert parse(SV, "sv", max_records=None).counts()["records"] == 4


class TestParserRuleSetRegistry:
    def test_the_current_version_is_registered_and_reproduces_its_own_documents(self):
        """The invariant a parser change must keep: every registered rule set rebuilds what it wrote."""
        assert CALLSET_PARSER_VERSION in PARSER_RULESETS
        for version in PARSER_RULESETS:
            for body, cls in ((SV, "sv"), (CNV, "cnv")):
                doc = json.loads(json.dumps(parse_with(version, body, cls).to_document()))
                assert verify_document_for_version(version, HEAD + body, doc) is True, version

    def test_an_unregistered_version_is_none_never_a_pass(self):
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        assert verify_document_for_version("structural_vcf/0", HEAD + SV, doc) is None

    def test_a_registered_version_reports_a_mismatch_as_false(self):
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        assert (
            verify_document_for_version(CALLSET_PARSER_VERSION, HEAD + SV.replace("END=5000", "END=6000"), doc) is False
        )

    def test_the_rule_set_recorded_is_the_one_used_even_when_a_newer_one_exists(self, monkeypatch):
        calls = []

        def v2(text, **kw):
            calls.append("v2")
            return PARSER_RULESETS[CALLSET_PARSER_VERSION](text, **kw)

        monkeypatch.setitem(PARSER_RULESETS, "structural_vcf/2", v2)
        doc = json.loads(json.dumps(parse(SV, "sv").to_document()))
        assert verify_document_for_version(CALLSET_PARSER_VERSION, HEAD + SV, doc) is True
        assert calls == []


def parse_with(version, body, cls):
    return PARSER_RULESETS[version](HEAD + body, assembly="GRCh38", record_class=cls)
