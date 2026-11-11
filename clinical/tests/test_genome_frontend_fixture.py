"""
The Chromosome Explorer's frontend tests run against a JSON fixture. This test
keeps that fixture from drifting away from the real backend: it must equal what
`build_chromosome_map` produces today, byte for byte.

THE FIXTURE IS SYNTHETIC. Every coordinate, caller name and identifier in it was
invented for the test; it is not patient data and not a real caller's output. It
is development/test material only and is not read by any production path.

Regenerate after an intended change to the map shape:
    GENOME_REGENERATE_FIXTURE=1 python -m pytest clinical/tests/test_genome_frontend_fixture.py
"""

import json
import os
import pathlib

from clinical.genome_analysis import build_chromosome_map, get_chromosome, parse_structural_vcf, summarise_run_document

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "chromosome-explorer"
    / "tests"
    / "fixtures"
    / "map.synthetic.json"
)

_HEADER = (
    "##fileformat=VCFv4.2\n##source=SYNTHETIC-TEST-CALLER\n"
    "##contig=<ID=1,length={a}>\n##contig=<ID=2,length={b}>\n##contig=<ID=X,length={x}>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
)

_CNV = (
    "1\t10000000\tcnv1\tN\t<DEL>\t80\tPASS\tSVTYPE=DEL;END=18000000;CN=1;CIPOS=-50000,50000\n"
    "1\t15000000\tcnv2\tN\t<DUP>\t20\tLowQual\tSVTYPE=DUP;END=30000000;CN=3\n"
    "1\t100000000\tcnv3\tN\t<CNV>\t.\tPASS\tSVTYPE=CNV;END=104000000\n"
)
_SV = (
    "1\t50000000\tsvd\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END=50400000;SVLEN=-400000;CIPOS=-300,300;CIEND=-300,300\n"
    "1\t60000000\tsvi\tN\t<INS>\t.\tPASS\tSVTYPE=INS;SVLEN=350\n"
    "1\t70000000\tsvv\tN\t<INV>\t.\tPASS\tSVTYPE=INV;END=72000000;IMPRECISE\n"
    "1\t80000000\tb1\tG\tG]2:5000000]\t.\tPASS\tSVTYPE=BND;MATEID=b2\n"
    "2\t5000000\tb2\tC\tC]1:80000000]\t.\tPASS\tSVTYPE=BND;MATEID=b1\n"
    "1\t90000000\tlone\tN\t<BND>\t.\tPASS\tSVTYPE=BND\n"
)


def build_fixture():
    a, b, x = (get_chromosome("GRCh38", c).length_bp for c in ("1", "2", "X"))
    head = _HEADER.format(a=a, b=b, x=x)
    cnv = parse_structural_vcf(
        head + _CNV, assembly="GRCh38", record_class="cnv", caller="SYNTHETIC-TEST-CALLER", call_set_id="synthetic-cnv"
    )
    sv = parse_structural_vcf(
        head + _SV, assembly="GRCh38", record_class="sv", caller="SYNTHETIC-TEST-CALLER", call_set_id="synthetic-sv"
    )
    summary = summarise_run_document(
        {
            "assembly": "GRCh38",
            "run_complete": True,
            "variants": [
                {"variant": {"chrom": c, "pos": p, "ref": "A", "alt": "T", "variant_type": "SNV"}}
                for c, p in (("1", 12_345_678), ("1", 51_000_000), ("17", 7_674_220))
            ],
        }
    )
    return {
        "_comment": "SYNTHETIC development/test fixture. Not patient data. Regenerate with GENOME_REGENERATE_FIXTURE=1.",
        "patient_id": "00000000-0000-0000-0000-000000000000",
        "maps": [
            {
                "interpretation_id": "00000000-0000-0000-0000-000000000001",
                "created_at": "2000-01-01T00:00:00+00:00",
                "report_states": ["draft"],
                "released_to_patient": False,
                "status": "ok",
                "reason": None,
                "map": build_chromosome_map(assembly="GRCh38", summary=summary, call_sets=[cnv, sv]),
            }
        ],
        "call_sets_note": "Call sets are drawn only on a map of their own assembly; none is converted between assemblies.",
    }


def test_the_frontend_fixture_equals_what_the_backend_builds():
    text = json.dumps(build_fixture(), indent=1, sort_keys=True) + "\n"
    if os.environ.get("GENOME_REGENERATE_FIXTURE"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text, encoding="utf-8", newline="\n")
    assert FIXTURE.exists(), "fixture missing; run with GENOME_REGENERATE_FIXTURE=1"
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == json.loads(text)


def test_the_fixture_exercises_every_class_the_explorer_draws():
    data = build_fixture()["maps"][0]["map"]
    c1 = next(c for c in data["chromosomes"] if c["name"] == "1")["layers"]
    assert {s["cnv_type"] for s in c1["cnv"]["segments"]} == {"deletion", "duplication", "copy_number_change"}
    assert {i["sv_type"] for i in c1["sv"]["intervals"]} == {"DEL", "INV"}
    assert {b["sv_type"] for b in c1["sv"]["breakends"]} == {"INS", "BND"}
    precisions = {s["start_breakpoint"]["precision"] for s in c1["cnv"]["segments"]} | {
        i["start"]["precision"] for i in c1["sv"]["intervals"]
    }
    assert precisions == {"precise", "interval", "imprecise_unquantified"}
    assert any(s["status"] == "uncertain" for s in c1["cnv"]["segments"]) and len(data["links"]) == 2
    assert next(c for c in data["chromosomes"] if c["name"] == "2")["layers"]["cnv"]["status"] == "no_calls_reported"
    assert (
        next(c for c in data["chromosomes"] if c["name"] == "X")["layers"]["small_variants"]["status"]
        == "no_variants_reported"
    )
