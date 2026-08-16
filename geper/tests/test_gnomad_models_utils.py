"""
Unit tests for pipeline/gnomad/models.py and pipeline/gnomad/utils.py.

Pure-function/dataclass tests only -- no network, no tabix subprocess,
no torch/tensorflow -- mirroring tests/test_mmsplice_utils.py.
"""

import unittest

from pipeline.gnomad.models import GnomadAnnotation, PopulationFrequency
from pipeline.gnomad.utils import (
    classify_variant_type,
    extract_global_counts_from_info,
    extract_population_counts_from_info,
    gnomad_variant_id,
    normalize_build,
    normalize_chrom,
    parse_vcf_info_field,
    variant_key,
)


class TestNormalizeBuild(unittest.TestCase):
    def test_grch38_spellings(self):
        for spelling in ("GRCh38", "grch38", "hg38", "GRCh-38".replace("-", ""), "b38"):
            self.assertEqual(normalize_build(spelling), "GRCh38")

    def test_grch37_spellings(self):
        for spelling in ("GRCh37", "grch37", "hg19", "b37"):
            self.assertEqual(normalize_build(spelling), "GRCh37")

    def test_none_defaults_to_grch38(self):
        self.assertEqual(normalize_build(None), "GRCh38")

    def test_unrecognized_defaults_to_grch38(self):
        self.assertEqual(normalize_build("nonsense"), "GRCh38")


class TestVariantClassification(unittest.TestCase):
    def test_snv(self):
        self.assertEqual(classify_variant_type("A", "T"), "SNV")

    def test_insertion(self):
        self.assertEqual(classify_variant_type("A", "AT"), "insertion")

    def test_deletion(self):
        self.assertEqual(classify_variant_type("AT", "A"), "deletion")


class TestChromAndKeys(unittest.TestCase):
    def test_normalize_chrom_adds_prefix(self):
        self.assertEqual(normalize_chrom("1", with_chr_prefix=True), "chr1")

    def test_normalize_chrom_strips_prefix(self):
        self.assertEqual(normalize_chrom("chr1", with_chr_prefix=False), "1")

    def test_normalize_chrom_idempotent(self):
        self.assertEqual(normalize_chrom("chr1", with_chr_prefix=True), "chr1")
        self.assertEqual(normalize_chrom("1", with_chr_prefix=False), "1")

    def test_variant_key_is_build_qualified(self):
        key38 = variant_key("chr1", 100, "A", "T", "GRCh38")
        key37 = variant_key("chr1", 100, "A", "T", "GRCh37")
        self.assertNotEqual(key38, key37)
        self.assertEqual(key38, "GRCh38:1:100:A:T")

    def test_gnomad_variant_id_format(self):
        self.assertEqual(gnomad_variant_id("chr1", 55516888, "G", "GA"), "1-55516888-G-GA")

    def test_nuclear_chromosomes_unaffected_by_mt_handling(self):
        for raw, expected_prefixed, expected_bare in (
            ("17", "chr17", "17"),
            ("chr17", "chr17", "17"),
            ("X", "chrX", "X"),
            ("chrX", "chrX", "X"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_chrom(raw, with_chr_prefix=True), expected_prefixed)
                self.assertEqual(normalize_chrom(raw, with_chr_prefix=False), expected_bare)


class TestMitochondrialChromNormalization(unittest.TestCase):
    """
    Round 29: `normalize_chrom`/`gnomad_variant_id`'s bare-strip used to
    map 'MT'/'chrMT' to the non-existent gnomAD contig 'chrMT' and
    'M'/'chrM' to bare 'M' (missing gnomAD's required 'chr' prefix on
    GRCh38) -- wrong for 2 of the 4 real-world spellings in each mode.
    gnomAD's own real convention is 'chrM' (GRCh38 site VCFs/browser)
    and bare 'M' (its GraphQL/mtDNA-dataset dash-joined variant IDs,
    e.g. 'M-3243-A-G') -- never 'MT', unlike Ensembl/NCBI Entrez.

    Currently unreachable in production (round 14/29's own
    ROUND_CANDIDATES.md entries: gnomAD's main integration has no mtDNA
    dataset wired in at all), fixed anyway for correctness. `variant_key`
    deliberately excluded -- confirmed round 15/29 to be a correctly
    bespoke internal cache key with no external target to match.
    """

    _MT_SPELLINGS = ("MT", "M", "chrM", "chrMT")

    def test_all_mt_spellings_produce_chrM_with_prefix(self):
        for spelling in self._MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_chrom(spelling, with_chr_prefix=True), "chrM")

    def test_all_mt_spellings_produce_bare_M_without_prefix(self):
        for spelling in self._MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_chrom(spelling, with_chr_prefix=False), "M")

    def test_all_mt_spellings_produce_bare_M_in_variant_id(self):
        for spelling in self._MT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(gnomad_variant_id(spelling, 3243, "A", "G"), "M-3243-A-G")

    def test_case_insensitive(self):
        for spelling in ("mt", "m", "Chrm", "CHRMT", "ChrM"):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_chrom(spelling, with_chr_prefix=True), "chrM")
                self.assertEqual(normalize_chrom(spelling, with_chr_prefix=False), "M")


class TestVcfInfoParsing(unittest.TestCase):
    def test_parse_vcf_info_field(self):
        info = parse_vcf_info_field("AC=5;AN=1000;AF=0.005;PASS_FLAG")
        self.assertEqual(info["AC"], "5")
        self.assertEqual(info["AN"], "1000")
        self.assertEqual(info["PASS_FLAG"], "True")

    def test_extract_global_counts(self):
        info = parse_vcf_info_field("AC=10;AN=2000;AF=0.005;nhomalt=1;AC_hemi=0")
        globals_ = extract_global_counts_from_info(info)
        self.assertEqual(globals_["ac"], 10)
        self.assertEqual(globals_["an"], 2000)
        self.assertAlmostEqual(globals_["af"], 0.005)
        self.assertEqual(globals_["hom"], 1)

    def test_extract_population_counts_all_populations_present(self):
        info = parse_vcf_info_field("AC_afr=5;AN_afr=100;AC_nfe=2;AN_nfe=200;nhomalt_nfe=0")
        pops = extract_population_counts_from_info(info)
        self.assertEqual(pops["afr"], (5, 100, None, None))
        self.assertEqual(pops["nfe"], (2, 200, 0, None))
        self.assertEqual(pops["eas"], (None, None, None, None))

    def test_remaining_population_accepts_oth_alias(self):
        info = parse_vcf_info_field("AC_oth=3;AN_oth=50")
        pops = extract_population_counts_from_info(info)
        self.assertEqual(pops["remaining"], (3, 50, None, None))


class TestPopulationFrequency(unittest.TestCase):
    def test_af_derived_from_ac_an_when_absent(self):
        freq = PopulationFrequency(population="afr", ac=10, an=100)
        self.assertAlmostEqual(freq.af, 0.1)

    def test_explicit_af_not_overwritten(self):
        freq = PopulationFrequency(population="afr", ac=10, an=100, af=0.5)
        self.assertEqual(freq.af, 0.5)

    def test_zero_an_yields_undefined_af_not_divide_by_zero(self):
        # an=0 means "zero alleles observed", which is a genuinely
        # undefined frequency (not 0.0) -- this only verifies no
        # ZeroDivisionError is raised, matching __post_init__'s
        # explicit `if self.an:` (falsy-zero) guard.
        freq = PopulationFrequency(population="afr", ac=0, an=0)
        self.assertIsNone(freq.af)

    def test_label_lookup(self):
        self.assertEqual(PopulationFrequency(population="nfe").label, "European (non-Finnish)")


class TestGnomadAnnotation(unittest.TestCase):
    def test_global_af_prefers_genome_over_exome(self):
        ann = GnomadAnnotation(
            chrom="1",
            pos=1,
            ref="A",
            alt="T",
            build="GRCh38",
            source="test",
            genome_af=0.01,
            exome_af=0.02,
        )
        self.assertEqual(ann.global_af, 0.01)

    def test_global_af_falls_back_to_exome(self):
        ann = GnomadAnnotation(chrom="1", pos=1, ref="A", alt="T", build="GRCh38", source="test", exome_af=0.02)
        self.assertEqual(ann.global_af, 0.02)

    def test_global_af_falls_back_to_ac_an(self):
        ann = GnomadAnnotation(chrom="1", pos=1, ref="A", alt="T", build="GRCh38", source="test", ac=5, an=100)
        self.assertEqual(ann.global_af, 0.05)

    def test_highest_population_resolution(self):
        ann = GnomadAnnotation(
            chrom="1",
            pos=1,
            ref="A",
            alt="T",
            build="GRCh38",
            source="test",
            population_breakdown={
                "afr": PopulationFrequency(population="afr", af=0.1),
                "nfe": PopulationFrequency(population="nfe", af=0.3),
            },
        )
        ann.annotate_highest_population()
        self.assertEqual(ann.highest_population, "nfe")

    def test_not_found_factory(self):
        ann = GnomadAnnotation.not_found("1", 1, "A", "T", "GRCh38", "local_index")
        self.assertFalse(ann.found)
        self.assertIsNone(ann.error)

    def test_from_error_factory(self):
        ann = GnomadAnnotation.from_error("1", 1, "A", "T", "GRCh38", "boom")
        self.assertFalse(ann.found)
        self.assertEqual(ann.error, "boom")

    def test_to_dict_is_json_serializable(self):
        import json

        ann = GnomadAnnotation(
            chrom="1",
            pos=1,
            ref="A",
            alt="T",
            build="GRCh38",
            source="test",
            found=True,
            genome_af=0.001,
            population_breakdown={"afr": PopulationFrequency(population="afr", af=0.002)},
        )
        json.dumps(ann.to_dict())  # must not raise


if __name__ == "__main__":
    unittest.main()
