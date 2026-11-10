"""
The Genome Analysis build aliases (clinical/genome_analysis/reference.py) must agree
with the pipeline's own assembly validator for every spelling they both accept.
Lives in geper/tests because it needs geper's `pipeline` package, which the
clinical test environment does not have.
"""

import pytest

from clinical.genome_analysis import canonical_assembly


@pytest.mark.parametrize("spelling", ["GRCh37", "hg19", "b37", "GRCh38", "hg38", "b38"])
def test_alias_parity_with_the_pipeline_assembly_validator(spelling):
    from pipeline import assembly_validator

    assert assembly_validator._normalize_build(spelling) == canonical_assembly(spelling).lower()
