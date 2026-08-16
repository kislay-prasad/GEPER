"""
Round 30: `utils/ncbi_eutils.py::parse_vcv_submitters` -- the pure XML
parser both `database/clinvar_client.py::ClinVarClient._fetch_submitters`
and `pipeline/ps1_pm5/lookup.py::ClinVarCodonLookup._fetch_submitters`
use to pull submitting-organisation identity out of ClinVar's
`efetch&rettype=vcv` XML response (not present in the `esummary` JSON
those two `_esummary` methods already parse -- see this function's own
docstring for the live-confirmed comparison).
"""

import unittest

from utils.ncbi_eutils import parse_vcv_submitters

_XML_TWO_VARIATIONS = """<?xml version="1.0"?>
<ClinVarResult-Set>
  <VariationArchive VariationID="12297" Accession="VCV000012297">
    <ClassifiedRecord>
      <ClinicalAssertionList>
        <ClinicalAssertion ID="1">
          <ClinVarAccession Accession="SCV000033337" SubmitterName="OMIM" OrgID="3"/>
        </ClinicalAssertion>
      </ClinicalAssertionList>
    </ClassifiedRecord>
  </VariationArchive>
  <VariationArchive VariationID="41804" Accession="VCV000041804">
    <ClassifiedRecord>
      <ClinicalAssertionList>
        <ClinicalAssertion ID="2">
          <ClinVarAccession Accession="SCV000111111" SubmitterName="ENIGMA" OrgID="500123"/>
        </ClinicalAssertion>
        <ClinicalAssertion ID="3">
          <ClinVarAccession Accession="SCV000222222" SubmitterName="Ambry Genetics" OrgID="500456"/>
        </ClinicalAssertion>
      </ClinicalAssertionList>
    </ClassifiedRecord>
  </VariationArchive>
</ClinVarResult-Set>"""


class TestParseVcvSubmitters(unittest.TestCase):
    def test_batched_response_keyed_by_variation_id(self):
        result = parse_vcv_submitters(_XML_TWO_VARIATIONS)
        self.assertEqual(set(result.keys()), {"12297", "41804"})
        self.assertEqual(result["12297"], [{"name": "OMIM", "org_id": "3", "scv": "SCV000033337"}])

    def test_multiple_submitters_on_one_variation_all_captured(self):
        result = parse_vcv_submitters(_XML_TWO_VARIATIONS)
        names = {s["name"] for s in result["41804"]}
        self.assertEqual(names, {"ENIGMA", "Ambry Genetics"})
        self.assertEqual(len(result["41804"]), 2)  # not collapsed to one

    def test_malformed_xml_returns_empty_dict_not_raises(self):
        self.assertEqual(parse_vcv_submitters("<not><valid"), {})

    def test_empty_string_returns_empty_dict(self):
        self.assertEqual(parse_vcv_submitters(""), {})

    def test_variation_with_no_assertions_yields_empty_submitter_list(self):
        xml = """<?xml version="1.0"?>
<ClinVarResult-Set>
  <VariationArchive VariationID="99" Accession="VCV000000099">
    <ClassifiedRecord><ClinicalAssertionList/></ClassifiedRecord>
  </VariationArchive>
</ClinVarResult-Set>"""
        self.assertEqual(parse_vcv_submitters(xml), {"99": []})


if __name__ == "__main__":
    unittest.main()
