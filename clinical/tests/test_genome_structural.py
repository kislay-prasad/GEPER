"""
Phase 2 Genome Analysis: CNV and structural-variant models, the external-VCF
ingestion interface, breakend pairing and the Chromosome Explorer map builder.
Pure: no database, no network. Every fixture below is SYNTHETIC and written for
these tests; none is a real patient's data or a real caller's output.
"""

import ast
import json
import math
import pathlib

import pytest

from clinical.genome_analysis import (
    CallSetRejected,
    StructuralValidationError,
    assign_lanes,
    build_chromosome_map,
    get_chromosome,
    pair_breakends,
    parse_structural_vcf,
    relate_cnvs,
    summarise_run_document,
)
from clinical.genome_analysis.structural import (
    PRECISION_INTERVAL,
    PRECISION_PRECISE,
    PRECISION_UNQUANTIFIED,
    Breakpoint,
    CopyNumberVariant,
    StructuralVariant,
)

L38 = {c: get_chromosome("GRCh38", c).length_bp for c in ("1", "2", "X", "MT")}
L37_1 = get_chromosome("GRCh37", "1").length_bp


def _vcf(*records, contigs=("1",), assembly="GRCh38", extra_header=(), samples=("S1",)):
    head = ["##fileformat=VCFv4.2", "##source=SyntheticTestCaller"]
    head += [f"##contig=<ID={c},length={get_chromosome(assembly, c).length_bp}>" for c in contigs]
    head += list(extra_header)
    cols = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO" + ("\tFORMAT\t" + "\t".join(samples) if samples else "")
    return "\n".join(head + [cols] + ["\t".join(r) for r in records]) + "\n"


def _rec(chrom, pos, vid, alt, info, *, qual="50", filt="PASS", ref="N", fmt=("GT", "0/1")):
    row = [chrom, str(pos), vid, ref, alt, qual, filt, info]
    if fmt:
        row += [fmt[0], fmt[1]]
    return tuple(row)


def cnv_set(*records, **kw):
    return parse_structural_vcf(
        _vcf(*records, **kw), assembly=kw.get("assembly", "GRCh38"), record_class="cnv", caller="synthetic"
    )


def sv_set(*records, **kw):
    return parse_structural_vcf(
        _vcf(*records, **kw), assembly=kw.get("assembly", "GRCh38"), record_class="sv", caller="synthetic"
    )


# ─── CNV ─────────────────────────────────────────────────────────────────────


class TestCnv:
    def test_valid_deletion_keeps_everything_the_source_said(self):
        cs = cnv_set(
            _rec("1", 1000, "c1", "<DEL>", "SVTYPE=DEL;END=5000;CN=1;CIPOS=-10,20;LOG2RATIO=-1.02;FOO=bar", qual="87.5")
        )
        (c,) = cs.cnvs
        assert (c.cnv_type, c.status, c.chrom, c.start, c.end, c.assembly) == (
            "deletion",
            "detected",
            "1",
            1000,
            5000,
            "GRCh38",
        )
        assert (c.copy_number, c.copy_number_source, c.log2_ratio, c.quality, c.filter) == (
            1.0,
            "INFO:CN",
            -1.02,
            87.5,
            "PASS",
        )
        assert c.start_breakpoint.precision == PRECISION_INTERVAL and (
            c.start_breakpoint.ci_low,
            c.start_breakpoint.ci_high,
        ) == (990, 1020)
        assert c.end_breakpoint.precision == PRECISION_PRECISE
        assert c.caller == "synthetic" and cs.caller_in_header == "SyntheticTestCaller" and c.source_id == "c1"
        assert c.evidence["info"]["FOO"] == "bar" and "SVTYPE=DEL" in c.evidence["raw_record"]
        assert c.evidence["format"] == {"S1": {"GT": "0/1"}} and c.line_no == 5 and c.length_bp == 4001

    def test_valid_duplication_and_unspecified_direction(self):
        cs = cnv_set(
            _rec("1", 100, "d", "<DUP>", "SVTYPE=DUP;END=900;CN=3"),
            _rec("1", 2000, "n", "<CNV>", "SVTYPE=CNV;END=2900;CN=1"),
        )
        dup, change = cs.cnvs
        assert dup.cnv_type == "duplication" and dup.copy_number == 3.0
        # direction is NEVER inferred from CN (loss is relative to an unknown baseline)
        assert change.cnv_type == "copy_number_change" and change.copy_number == 1.0

    def test_missing_copy_number_is_none_never_two(self):
        (c,) = cnv_set(_rec("1", 100, "d", "<DEL>", "SVTYPE=DEL;END=900")).cnvs
        assert c.copy_number is None and c.copy_number_source is None and c.log2_ratio is None
        d = c.to_dict()
        assert d["copy_number"] is None and d["log2_ratio"] is None

    def test_cn_can_come_from_alt_or_a_single_samples_format(self):
        (a,) = cnv_set(_rec("1", 100, "a", "<CN0>", "END=900")).cnvs
        assert a.cnv_type == "copy_number_change" and a.copy_number == 0.0 and a.copy_number_source == "ALT"
        (f,) = cnv_set(_rec("1", 100, "f", "<DEL>", "SVTYPE=DEL;END=900", fmt=("GT:CN", "0/1:1"))).cnvs
        assert f.copy_number == 1.0 and f.copy_number_source == "FORMAT:CN"
        two = cnv_set(
            _rec("1", 100, "t", "<DEL>", "SVTYPE=DEL;END=900", fmt=("GT:CN", "0/1:1\t0/0:2")), samples=("S1", "S2")
        )
        assert two.cnvs[0].copy_number is None  # two samples: not guessed, both stay in evidence
        assert two.cnvs[0].evidence["format"]["S2"]["CN"] == "2"

    def test_alt_and_cn_that_disagree_is_invalid(self):
        cs = cnv_set(_rec("1", 100, "x", "<CN3>", "END=900;CN=2"))
        assert cs.cnvs == () and cs.outcomes[0].status == "invalid" and "CN=2" in cs.outcomes[0].reason

    def test_missing_confidence_is_allowed_and_not_promoted(self):
        (c,) = cnv_set(_rec("1", 100, "d", "<DEL>", "SVTYPE=DEL;END=900", qual=".", filt=".")).cnvs
        assert c.quality is None and c.filter is None and c.status == "uncertain"

    @pytest.mark.parametrize("filt,expected", [("PASS", "detected"), ("LowQual", "uncertain"), (".", "uncertain")])
    def test_only_pass_is_detected(self, filt, expected):
        (c,) = cnv_set(_rec("1", 100, "d", "<DEL>", "SVTYPE=DEL;END=900", filt=filt)).cnvs
        assert c.status == expected

    def test_imprecision_is_positional_not_a_downgrade(self):
        (c,) = cnv_set(_rec("1", 100, "d", "<DEL>", "SVTYPE=DEL;END=900;IMPRECISE")).cnvs
        assert c.status == "detected" and c.start_breakpoint.precision == PRECISION_UNQUANTIFIED

    @pytest.mark.parametrize(
        "info,why",
        [
            ("SVTYPE=DEL;END=50", "precedes"),  # end before start
            (f"SVTYPE=DEL;END={L38['1'] + 5}", "outside"),  # beyond chromosome end
            ("SVTYPE=DEL", "neither END"),
            ("SVTYPE=DEL;END=abc", "not an integer"),
            ("SVTYPE=DEL;END=900;CN=-1", "negative"),
            ("SVTYPE=DEL;END=900;CN=abc", "not a number"),
            ("SVTYPE=DEL;END=900;CN=nan", "not finite"),
            ("SVTYPE=DUP;END=900", "contradicts"),
            ("SVTYPE=DEL;END=900;CIPOS=5,1", "low exceeds high"),
            ("SVTYPE=DEL;END=900;CIPOS=oops", "low,high"),
        ],
    )
    def test_invalid_records_say_why(self, info, why):
        cs = cnv_set(_rec("1", 100, "bad", "<DEL>", info))
        assert cs.cnvs == () and cs.outcomes[0].status == "invalid" and why in cs.outcomes[0].reason, cs.outcomes[0]

    def test_invalid_position_columns(self):
        for pos in ("0", "-5", "abc", "1.5"):
            cs = cnv_set(("1", pos, "p", "N", "<DEL>", "50", "PASS", "END=900", "GT", "0/1"))
            assert cs.outcomes[0].status == "invalid", pos
        short = parse_structural_vcf(_vcf() + "1\t100\tshort\n", assembly="GRCh38", record_class="cnv")
        assert short.outcomes[0].status == "invalid" and "columns" in short.outcomes[0].reason

    def test_unsupported_is_distinct_from_invalid(self):
        cs = cnv_set(
            _rec("1", 100, "inv", "<INV>", "SVTYPE=INV;END=900"),
            _rec("1", 100, "snv", "T", "DP=10", ref="A"),
            _rec("1", 100, "multi", "<DEL>,<DUP>", "END=900"),
            _rec("chrUn_gl000220", 100, "un", "<DEL>", "END=900"),
        )
        assert [o.status for o in cs.outcomes] == ["unsupported"] * 4
        assert cs.cnvs == () and len(cs.rejected) == 4

    def test_one_outcome_per_record_in_order(self):
        cs = cnv_set(
            _rec("1", 100, "a", "<DEL>", "END=900"),
            _rec("1", 100, "b", "<DEL>", "END=10"),
            _rec("1", 100, "c", "<INV>", "END=900"),
        )
        assert [(o.line_no, o.status) for o in cs.outcomes] == [(5, "detected"), (6, "invalid"), (7, "unsupported")]
        assert [o.source_id for o in cs.outcomes] == ["a", "b", "c"] and cs.counts()["records"] == 3

    def test_overlapping_and_adjacent_cnvs_are_all_kept(self):
        cs = cnv_set(
            _rec("1", 100, "a", "<DEL>", "SVTYPE=DEL;END=500"),
            _rec("1", 400, "b", "<DUP>", "SVTYPE=DUP;END=800"),
            _rec("1", 801, "c", "<DEL>", "SVTYPE=DEL;END=900"),
            _rec("1", 5000, "d", "<DEL>", "SVTYPE=DEL;END=5100"),
        )
        assert len(cs.cnvs) == 4  # nothing merged
        rel = relate_cnvs(cs.cnvs)
        assert {(r["a"], r["b"], r["relation"]) for r in rel} == {(0, 1, "overlap"), (1, 2, "adjacent")}
        assert assign_lanes([(c.start, c.end) for c in cs.cnvs]) == [0, 1, 0, 0]

    def test_exact_duplicate_records_both_survive(self):
        r = _rec("1", 100, "same", "<DEL>", "SVTYPE=DEL;END=500")
        assert len(cnv_set(r, r).cnvs) == 2

    def test_grch37_and_grch38_are_distinguished_by_length(self):
        pos = L38["1"] + 100_000  # valid on GRCh37 chr1 only
        ok37 = parse_structural_vcf(
            _vcf(_rec("1", 100, "a", "<DEL>", f"END={pos}"), assembly="GRCh37"), assembly="GRCh37", record_class="cnv"
        )
        assert ok37.cnvs[0].assembly == "GRCh37" and ok37.cnvs[0].end == pos
        no38 = parse_structural_vcf(
            _vcf(_rec("1", 100, "a", "<DEL>", f"END={pos}")), assembly="GRCh38", record_class="cnv"
        )
        assert no38.cnvs == () and no38.outcomes[0].status == "invalid" and "GRCh38" in no38.outcomes[0].reason

    def test_header_that_contradicts_the_declared_build_rejects_the_whole_file(self):
        text38 = _vcf(_rec("1", 100, "a", "<DEL>", "END=500"))
        with pytest.raises(CallSetRejected, match="GRCh38 length"):
            parse_structural_vcf(text38, assembly="GRCh37", record_class="cnv")
        ref = _vcf(
            _rec("1", 100, "a", "<DEL>", "END=500"), contigs=(), extra_header=("##reference=file:///refs/hg19.fa",)
        )
        with pytest.raises(CallSetRejected, match="header names GRCh37"):
            parse_structural_vcf(ref, assembly="GRCh38", record_class="cnv")

    def test_a_header_with_no_evidence_is_accepted_but_recorded_as_unverified(self):
        cs = parse_structural_vcf(
            _vcf(_rec("1", 100, "a", "<DEL>", "END=500"), contigs=()), assembly="hg38", record_class="cnv"
        )
        assert cs.header_check["status"] == "unverified" and cs.assembly == "GRCh38"
        verified = cnv_set(_rec("1", 100, "a", "<DEL>", "END=500"))
        assert verified.header_check["status"] == "verified" and verified.header_check["contigs_checked"] == 1

    @pytest.mark.parametrize("assembly", ["", "GRCh39", "T2T-CHM13", "hg38_decoy"])
    def test_other_assemblies_and_liftover_are_refused(self, assembly):
        with pytest.raises(CallSetRejected, match="liftover"):
            parse_structural_vcf(_vcf(), assembly=assembly, record_class="cnv")

    def test_file_level_problems_reject(self):
        with pytest.raises(CallSetRejected):
            parse_structural_vcf("1\t100\t.\tN\t<DEL>\t.\t.\tEND=5\n", assembly="GRCh38", record_class="cnv")
        with pytest.raises(CallSetRejected):
            parse_structural_vcf(
                "##x\n1\t100\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n", assembly="GRCh38", record_class="cnv"
            )
        with pytest.raises(CallSetRejected):
            parse_structural_vcf(_vcf(), assembly="GRCh38", record_class="banana")

    def test_model_validates_direct_construction(self):
        base = dict(assembly="GRCh38", chrom="1", start=10, end=20, cnv_type="deletion", status="detected")
        CopyNumberVariant(**base)
        for patch, msg in (
            (dict(assembly="hg38"), "unsupported assembly"),
            (dict(chrom="chrUn"), "not a primary"),
            (dict(start=0), "outside"),
            (dict(end=5), "precedes"),
            (dict(status="not_assessed"), "status"),
            (dict(cnv_type="loss"), "unsupported CNV type"),
            (dict(copy_number=-1), "negative"),
            (dict(log2_ratio=math.inf), "finite"),
            (dict(copy_number=True), "finite"),
        ):
            with pytest.raises(StructuralValidationError, match=msg):
                CopyNumberVariant(**{**base, **patch})


# ─── structural variants ─────────────────────────────────────────────────────


class TestSv:
    def test_deletion_duplication_inversion_are_two_breakpoints_on_one_chromosome(self):
        cs = sv_set(
            _rec("1", 1000, "d", "<DEL>", "SVTYPE=DEL;END=2000;SVLEN=-1000"),
            _rec("1", 3000, "u", "<DUP:TANDEM>", "SVTYPE=DUP;END=4000;SVLEN=1000"),
            _rec("1", 5000, "v", "<INV>", "SVTYPE=INV;END=6000"),
        )
        d, u, v = cs.svs
        assert [s.sv_type for s in cs.svs] == ["DEL", "DUP", "INV"] and all(s.status == "detected" for s in cs.svs)
        assert (d.breakpoints[0].pos, d.breakpoints[1].pos, d.sv_length) == (1000, 2000, -1000)
        assert all(len(s.breakpoints) == 2 and s.is_interchromosomal is False for s in cs.svs)
        assert u.sv_type == "DUP" and v.breakpoints[1].pos == 6000

    def test_insertion_has_one_breakpoint_and_its_length_is_kept_not_invented(self):
        cs = sv_set(
            _rec("1", 100, "i1", "<INS>", "SVTYPE=INS;SVLEN=300"),
            _rec("1", 200, "i2", "ACGTACGT", "SVTYPE=INS;SVLEN=7", ref="A"),
            _rec("1", 300, "i3", "<INS:ME:ALU>", "SVTYPE=INS"),
        )
        a, b, c = cs.svs
        assert len(a.breakpoints) == 1 and a.sv_length == 300 and a.inserted_sequence is None
        assert b.inserted_sequence == "ACGTACGT" and c.sv_length is None and c.is_interchromosomal is None

    def test_breakpoint_uncertainty_is_a_range(self):
        (s,) = sv_set(_rec("1", 1000, "d", "<DEL>", "SVTYPE=DEL;END=2000;IMPRECISE;CIPOS=-50,50;CIEND=-5,30")).svs
        a, b = s.breakpoints
        assert (a.precision, a.ci_low, a.ci_high) == ("interval", 950, 1050)
        assert (b.precision, b.ci_low, b.ci_high) == ("interval", 1995, 2030)
        (u,) = sv_set(_rec("1", 1000, "d", "<DEL>", "SVTYPE=DEL;END=2000;IMPRECISE")).svs
        assert (
            u.breakpoints[0].precision == "imprecise_unquantified"
            and u.breakpoints[0].ci_low == u.breakpoints[0].ci_high == 1000
        )

    def test_paired_breakends_both_locations_preserved(self):
        cs = sv_set(
            _rec("1", 1000, "b1", "G]2:5000]", "SVTYPE=BND;MATEID=b2;EVENT=ev1"),
            _rec("2", 5000, "b2", "C]1:1000]", "SVTYPE=BND;MATEID=b1;EVENT=ev1"),
            contigs=("1", "2"),
        )
        a, b = cs.svs
        assert (a.breakpoints[0].chrom, a.breakpoints[0].pos, a.breakpoints[1].chrom, a.breakpoints[1].pos) == (
            "1",
            1000,
            "2",
            5000,
        )
        assert (b.breakpoints[0].chrom, b.breakpoints[1].chrom) == ("2", "1")
        assert a.breakpoints[0].notation == "t]p]" and a.partner_status == "partner_given" and a.event_id == "ev1"
        assert a.is_interchromosomal is True  # a fact about the two ends, NOT a claim of translocation
        pairing = pair_breakends(cs.svs)
        assert (
            pairing.paired == ((0, 1),)
            and not pairing.mate_missing
            and not pairing.inconsistent
            and not pairing.unpaired
        )
        assert len(cs.svs) == 2  # not merged

    @pytest.mark.parametrize(
        "alt,form", [("T[2:500[", "t[p["), ("T]2:500]", "t]p]"), ("]2:500]T", "]p]t"), ("[2:500[T", "[p[t")]
    )
    def test_all_four_bracket_forms(self, alt, form):
        (s,) = sv_set(_rec("1", 100, "b", alt, "SVTYPE=BND"), contigs=("1", "2")).svs
        assert s.breakpoints[0].notation == form and s.breakpoints[1].pos == 500 and s.sv_type == "BND"

    def test_intrachromosomal_bnd_is_not_called_interchromosomal(self):
        (s,) = sv_set(_rec("1", 100, "b", "T[1:900[", "SVTYPE=BND")).svs
        assert s.is_interchromosomal is False

    def test_missing_mate_is_reported_not_fabricated(self):
        cs = sv_set(_rec("1", 1000, "b1", "G]2:5000]", "SVTYPE=BND;MATEID=b2"), contigs=("1", "2"))
        pairing = pair_breakends(cs.svs)
        assert pairing.mate_missing == (0,) and pairing.paired == ()
        none_named = sv_set(_rec("1", 1000, "b1", "G]2:5000]", "SVTYPE=BND"), contigs=("1", "2"))
        assert pair_breakends(none_named.svs).unpaired == (0,)

    def test_mates_that_disagree_are_flagged_inconsistent(self):
        cs = sv_set(
            _rec("1", 1000, "b1", "G]2:5000]", "SVTYPE=BND;MATEID=b2"),
            _rec("2", 5001, "b2", "C]1:1000]", "SVTYPE=BND;MATEID=b1"),  # 5001, not 5000
            contigs=("1", "2"),
        )
        assert pair_breakends(cs.svs).inconsistent == ((0, 1),) and pair_breakends(cs.svs).paired == ()

    def test_symbolic_bnd_without_partner_keeps_it_unstated(self):
        (s,) = sv_set(_rec("1", 100, "b", "<BND>", "SVTYPE=BND")).svs
        assert s.partner_status == "partner_not_specified" and len(s.breakpoints) == 1 and s.status == "uncertain"
        assert any("partner" in w for w in s.warnings)
        (t,) = sv_set(_rec("1", 100, "b", "<BND>", "SVTYPE=BND;CHR2=2;POS2=77"), contigs=("1", "2")).svs
        assert (t.breakpoints[1].chrom, t.breakpoints[1].pos, t.partner_status) == ("2", 77, "partner_given")

    def test_single_breakend_has_no_partner(self):
        (s,) = sv_set(_rec("1", 100, "b", "A.", "SVTYPE=BND")).svs
        assert s.partner_status == "single_breakend" and len(s.breakpoints) == 1

    def test_partner_on_an_unplaced_contig_is_kept_but_unplaced(self):
        (s,) = sv_set(_rec("1", 100, "b", "T[GL000220.1:500[", "SVTYPE=BND")).svs
        p = s.breakpoints[1]
        assert (p.chrom, p.chrom_raw, p.pos, s.partner_status) == (
            None,
            "GL000220.1",
            500,
            "partner_on_unplaced_contig",
        )
        assert s.is_interchromosomal is None and s.status == "uncertain"

    def test_partner_beyond_chromosome_end_is_invalid(self):
        cs = sv_set(
            _rec("1", 100, "b", f"T[2:{get_chromosome('GRCh38', '2').length_bp + 1}[", "SVTYPE=BND"), contigs=("1", "2")
        )
        assert cs.svs == () and cs.outcomes[0].status == "invalid" and "beyond the end" in cs.outcomes[0].reason

    def test_tra_needs_a_stated_partner(self):
        ok = sv_set(_rec("1", 100, "t", "<TRA>", "SVTYPE=TRA;CHR2=2;POS2=900"), contigs=("1", "2"))
        assert ok.svs[0].sv_type == "TRA" and ok.svs[0].breakpoints[1].chrom == "2"
        bad = sv_set(_rec("1", 100, "t", "<TRA>", "SVTYPE=TRA"))
        assert bad.svs == () and bad.outcomes[0].status == "invalid" and "not guessed" in bad.outcomes[0].reason

    def test_unsupported_sv_classes_and_wrong_class_records(self):
        cs = sv_set(
            _rec("1", 100, "c", "<CPX>", "SVTYPE=CPX;END=500"),
            _rec("1", 100, "cn", "<CNV>", "SVTYPE=CNV;END=500"),
            _rec("1", 100, "cn2", "<CN3>", "END=500"),
            _rec("1", 100, "snv", "T", "DP=4", ref="A"),
            _rec("1", 100, "m", "<DEL>,<DUP>", "END=500"),
            _rec("chrUn_x", 100, "u", "<DEL>", "END=500"),
        )
        assert [o.status for o in cs.outcomes] == ["unsupported"] * 6 and cs.svs == ()
        assert "CPX" in cs.outcomes[0].reason and "copy-number" in cs.outcomes[1].reason

    def test_malformed_records_are_invalid_not_unsupported(self):
        cs = sv_set(
            _rec("1", 100, "a", "<DEL>", "SVTYPE=DEL"),  # no extent
            _rec("1", 100, "b", "<DEL>", "SVTYPE=INV;END=500"),  # contradiction
            _rec("1", 900, "c", "<DEL>", "SVTYPE=DEL;END=100"),  # end before start
            _rec("1", 100, "d", "<DEL>", "SVTYPE=DEL;END=500;CHR2=2"),  # DEL cannot span chromosomes
            _rec("1", 100, "e", "<DEL>", "SVTYPE=DEL;END=500;SVLEN=x"),
            _rec("1", 100, "f", "T[2:9[", "SVTYPE=DEL;END=500"),
            _rec("1", L38["1"] + 1, "g", "<INS>", "SVTYPE=INS"),
            contigs=("1", "2"),
        )
        assert [o.status for o in cs.outcomes] == ["invalid"] * 7 and cs.svs == ()

    def test_svlen_disagreement_downgrades_to_uncertain_with_a_warning(self):
        (s,) = sv_set(_rec("1", 1000, "d", "<DEL>", "SVTYPE=DEL;END=2000;SVLEN=-1500")).svs
        assert s.status == "uncertain" and any("SVLEN" in w for w in s.warnings)

    def test_end_derived_from_svlen_is_disclosed_and_not_detected(self):
        (s,) = sv_set(_rec("1", 1000, "d", "<DEL>", "SVTYPE=DEL;SVLEN=-500")).svs
        assert s.breakpoints[1].pos == 1500 and s.status == "uncertain" and any("derived" in w for w in s.warnings)

    def test_several_mateids_keep_the_first_and_say_so(self):
        (s,) = sv_set(_rec("1", 100, "b", "T[1:900[", "SVTYPE=BND;MATEID=m1,m2")).svs
        assert s.mate_id == "m1" and any("MATEID" in w for w in s.warnings)

    def test_genotype_and_quality_fields_are_preserved_uninterpreted(self):
        (s,) = sv_set(
            _rec("1", 100, "d", "<DEL>", "SVTYPE=DEL;END=500;PE=12;SR=4", qual="33", fmt=("GT:GQ:PR", "1/1:48:3,9"))
        ).svs
        assert s.quality == 33.0 and s.evidence["format"]["S1"] == {"GT": "1/1", "GQ": "48", "PR": "3,9"}
        assert s.evidence["info"]["PE"] == "12" and s.caller == "synthetic" and s.call_set_id is None

    def test_a_vcf_with_no_genotype_columns_parses(self):
        cs = sv_set(("1", "100", "d", "N", "<DEL>", "50", "PASS", "SVTYPE=DEL;END=500"), samples=())
        assert len(cs.svs) == 1 and cs.svs[0].evidence["format"] == {}

    def test_model_validates_direct_construction(self):
        bp = Breakpoint("1", "1", 10, 10, 10)
        base = dict(assembly="GRCh38", sv_type="INS", status="detected", breakpoints=(bp,))
        StructuralVariant(**base)
        for patch, msg in (
            (dict(sv_type="CPX"), "unsupported SV type"),
            (dict(breakpoints=()), "one or two"),
            (dict(sv_type="DEL"), "two breakpoints"),
            (dict(sv_type="INS", breakpoints=(bp, bp)), "exactly one"),
            (dict(status="unsupported"), "status"),
            (dict(assembly="GRCh39"), "unsupported assembly"),
        ):
            with pytest.raises(StructuralValidationError, match=msg):
                StructuralVariant(**{**base, **patch})
        with pytest.raises(StructuralValidationError):
            Breakpoint("1", "1", 10, 11, 12)  # interval must contain pos
        with pytest.raises(StructuralValidationError):
            Breakpoint("1", "1", 10, 9, 11, "precise")  # a precise breakpoint has no interval


# ─── the map builder ─────────────────────────────────────────────────────────


def _summary(variants=(("chr1", 500),), assembly="GRCh38", complete=True):
    return summarise_run_document(
        {
            "assembly": assembly,
            "run_complete": complete,
            "variants": [
                {"variant": {"chrom": c, "pos": p, "ref": "A", "alt": "T", "variant_type": "SNV"}} for c, p in variants
            ],
        }
    )


def _chrom(m, name):
    return next(c for c in m["chromosomes"] if c["name"] == name)


class TestMap:
    def test_no_call_sets_means_not_assessed_never_normal(self):
        m = build_chromosome_map(assembly="GRCh38", summary=_summary(), call_sets=())
        assert len(m["chromosomes"]) == 25 and m["karyotype"]["status"] == "not_assessed"
        for c in m["chromosomes"]:
            assert c["layers"]["cnv"]["status"] == "not_assessed" and c["layers"]["sv"]["status"] == "not_assessed"
        assert _chrom(m, "1")["layers"]["small_variants"]["status"] == "variants_present"
        assert _chrom(m, "2")["layers"]["small_variants"]["status"] == "no_variants_reported"
        text = json.dumps({"chromosomes": m["chromosomes"], "karyotype": m["karyotype"], "links": m["links"]}).lower()
        for banned in ("diploid", "46,xx", "46,xy", '"normal"', "no abnormal"):
            assert banned not in text, banned

    def test_a_call_set_without_calls_on_a_chromosome_says_no_calls_reported(self):
        cs = cnv_set(_rec("1", 1000, "a", "<DEL>", "SVTYPE=DEL;END=5000;CN=1"))
        m = build_chromosome_map(assembly="GRCh38", summary=None, call_sets=[cs])
        assert _chrom(m, "1")["layers"]["cnv"]["status"] == "findings_present"
        assert _chrom(m, "2")["layers"]["cnv"]["status"] == "no_calls_reported"
        assert _chrom(m, "2")["layers"]["sv"]["status"] == "not_assessed"  # no SV call set at all
        assert _chrom(m, "1")["layers"]["small_variants"]["status"] == "not_assessed"  # no summary

    def test_cnv_segments_carry_scale_lanes_evidence_and_overlaps(self):
        cs = cnv_set(
            _rec("1", 1000, "a", "<DEL>", "SVTYPE=DEL;END=5000;CN=1;CIPOS=-100,100", filt="LowQual"),
            _rec("1", 3000, "b", "<DUP>", "SVTYPE=DUP;END=9000"),
        )
        m = build_chromosome_map(assembly="GRCh38", summary=None, call_sets=[cs])
        layer = _chrom(m, "1")["layers"]["cnv"]
        a, b = layer["segments"]
        assert (a["lane"], b["lane"]) == (0, 1) and a["status"] == "uncertain"
        assert a["x0"] == pytest.approx(1000 / L38["1"], abs=1e-8) and a["x1"] > a["x0"]
        assert (
            a["start_breakpoint"]["ci_low_frac"] < a["start_breakpoint"]["frac"] < a["start_breakpoint"]["ci_high_frac"]
        )
        assert a["evidence"]["raw_record"] and a["copy_number"] == 1.0 and b["copy_number"] is None
        assert layer["overlaps"] == [{"a": a["id"], "b": b["id"], "relation": "overlap"}]
        assert a["id"] != b["id"] and a["kind_class"] == "deletion" and b["kind_class"] == "duplication"

    def test_sv_layers_intervals_breakends_and_links(self):
        cs = sv_set(
            _rec("1", 1000, "d", "<DEL>", "SVTYPE=DEL;END=2000;CIPOS=-10,10"),
            _rec("1", 3000, "i", "<INS>", "SVTYPE=INS;SVLEN=40"),
            _rec("1", 4000, "b1", "G]2:5000]", "SVTYPE=BND;MATEID=b2"),
            _rec("2", 5000, "b2", "C]1:4000]", "SVTYPE=BND;MATEID=b1"),
            _rec("1", 6000, "lone", "<BND>", "SVTYPE=BND"),
            contigs=("1", "2"),
        )
        m = build_chromosome_map(assembly="GRCh38", summary=None, call_sets=[cs])
        one, two = _chrom(m, "1")["layers"]["sv"], _chrom(m, "2")["layers"]["sv"]
        assert [i["sv_type"] for i in one["intervals"]] == ["DEL"] and one["intervals"][0]["start"][
            "precision"
        ] == "interval"
        kinds = [(b["sv_type"], b["end"]) for b in one["breakends"]]
        assert ("INS", "own") in kinds and ("BND", "own") in kinds and ("BND", "partner") in kinds
        lone = next(b for b in one["breakends"] if b["partner_status"] == "partner_not_specified")
        assert lone["kind_class"] == "breakend"
        assert {(b["sv_type"], b["end"]) for b in two["breakends"]} == {("BND", "own"), ("BND", "partner")}
        links = m["links"]
        assert len(links) == 2 and all(x["is_interchromosomal"] is True and x["mate_paired"] for x in links)
        assert all(x["sv_type"] == "BND" for x in links)  # carried through; never relabelled translocation
        assert m["breakend_pairing"]["paired_pairs"] == 1 and m["breakend_pairing"]["mate_missing"] == 0

    def test_call_sets_of_another_assembly_are_listed_not_drawn(self):
        cs37 = parse_structural_vcf(
            _vcf(_rec("1", 1000, "a", "<DEL>", "END=5000"), assembly="GRCh37"),
            assembly="GRCh37",
            record_class="cnv",
            call_set_id="old",
        )
        m = build_chromosome_map(assembly="GRCh38", summary=_summary(), call_sets=[cs37])
        assert _chrom(m, "1")["layers"]["cnv"]["status"] == "not_assessed"
        assert (
            m["excluded_call_sets"][0]["assembly"] == "GRCh37"
            and "not converted" in m["excluded_call_sets"][0]["reason"]
        )
        assert m["call_sets"] == []

    def test_a_summary_for_another_build_is_refused(self):
        with pytest.raises(ValueError, match="different assembly"):
            build_chromosome_map(assembly="GRCh37", summary=_summary(assembly="GRCh38"), call_sets=())

    def test_sex_chromosomes_can_be_left_out_and_one_chromosome_selected(self):
        m = build_chromosome_map(assembly="GRCh38", summary=_summary(), include_sex_chromosomes=False)
        assert [c["name"] for c in m["chromosomes"]] == [str(n) for n in range(1, 23)] + ["MT"]
        one = build_chromosome_map(assembly="GRCh38", summary=_summary(), only_chromosome="17")
        assert [c["name"] for c in one["chromosomes"]] == ["17"]

    def test_region_restricts_every_layer_to_overlapping_findings(self):
        cnv = cnv_set(_rec("1", 1000, "a", "<DEL>", "END=2000"), _rec("1", 50_000, "b", "<DEL>", "END=60000"))
        sv = sv_set(_rec("1", 1500, "i", "<INS>", "SVTYPE=INS"), _rec("1", 70_000, "j", "<INS>", "SVTYPE=INS"))
        m = build_chromosome_map(
            assembly="GRCh38",
            summary=_summary(variants=(("1", 1200), ("1", 90_000))),
            call_sets=[cnv, sv],
            only_chromosome="1",
            region=(1100, 1900),
        )
        layers = _chrom(m, "1")["layers"]
        assert [s["start"] for s in layers["cnv"]["segments"]] == [1000]
        assert [b["breakpoint"]["pos"] for b in layers["sv"]["breakends"]] == [1500]
        assert [mk["pos"] for mk in layers["small_variants"]["markers"]] == [1200]
        with pytest.raises(ValueError):
            build_chromosome_map(assembly="GRCh38", summary=None, region=(1, 2))

    def test_every_finding_keeps_its_source_provenance(self):
        cs = parse_structural_vcf(
            _vcf(_rec("1", 1000, "a", "<DEL>", "END=2000")),
            assembly="GRCh38",
            record_class="cnv",
            caller="CallerX",
            source_id="file-7",
            call_set_id="cs-1",
        )
        m = build_chromosome_map(assembly="GRCh38", summary=None, call_sets=[cs])
        seg = _chrom(m, "1")["layers"]["cnv"]["segments"][0]
        assert (seg["caller"], seg["call_set_id"], seg["id"], seg["assembly"]) == (
            "CallerX",
            "cs-1",
            "cnv:cs-1:5",
            "GRCh38",
        )
        meta = m["call_sets"][0]
        assert (meta["caller"], meta["caller_in_header"], meta["source_id"], meta["assembly"]) == (
            "CallerX",
            "SyntheticTestCaller",
            "file-7",
            "GRCh38",
        )
        assert meta["counts"]["records"] == 1 and meta["header_check"]["status"] == "verified"

    def test_legend_distinguishes_every_class_and_limitations_are_stated(self):
        m = build_chromosome_map(assembly="GRCh38", summary=None)
        kinds = {item["kind"] for item in m["legend"]}
        assert {"small_variant", "deletion", "duplication", "insertion", "inversion", "breakend", "link"} <= kinds
        assert len({item["glyph"] for item in m["legend"]}) == len(m["legend"])
        assert any("not shown as normal" in t.lower() for t in m["limitations"]) and any(
            "translocation" in t for t in m["limitations"]
        )


class TestHygiene:
    def test_new_modules_are_pure(self):
        stdlib_ok = {"__future__", "dataclasses", "typing", "math", "re"}
        pkg = pathlib.Path(__file__).resolve().parent.parent / "genome_analysis"
        for path in pkg.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    assert all(a.name.split(".")[0] in stdlib_ok for a in node.names), (path.name, node.names)
                elif isinstance(node, ast.ImportFrom):
                    assert node.level == 1 or (node.module or "").split(".")[0] in stdlib_ok, (path.name, node.module)
