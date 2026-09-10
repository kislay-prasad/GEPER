"""Regression tests for: large-VCF scalability (item 3).

Confirms:
  - _iter_vcf is a true generator (not list-backed) so lazy consumers don't
    hold every record in memory at once.
  - _parse_vcf (list-returning compat wrapper) still produces identical
    results to before the refactor.
  - The annotation.json writer streams instead of building one giant
    in-memory JSON string, while producing byte-equivalent JSON content.
"""

import inspect
import json


SAMPLE_VCF = """##fileformat=VCFv4.2
##contig=<ID=chr1,length=248956422>
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE
chr1\t100\t.\tA\tT\t100\tPASS\t.\tGT\t0/1
chr1\t200\t.\tG\tC\t100\tPASS\t.\tGT\t1/1
chr1\t300\t.\tC\tG,T\t100\tPASS\t.\tGT\t1/2
"""


def _write_vcf(tmp_path):
    p = tmp_path / "sample.vcf"
    p.write_text(SAMPLE_VCF)
    return str(p)


def test_iter_vcf_is_a_generator():
    from pipeline.annotation.stage import _iter_vcf

    assert inspect.isgeneratorfunction(_iter_vcf)


def test_iter_vcf_matches_parse_vcf(tmp_path):
    from pipeline.annotation.stage import _iter_vcf, _parse_vcf

    vcf = _write_vcf(tmp_path)
    streamed = list(_iter_vcf(vcf))
    listed = _parse_vcf(vcf)
    assert len(streamed) == len(listed) == 4  # multi-allelic record expands to 2
    for a, b in zip(streamed, listed):
        assert (a.chrom, a.pos, a.ref, a.alt) == (b.chrom, b.pos, b.ref, b.alt)


def test_iter_vcf_lazy_consumption_does_not_retain_all_records(tmp_path):
    """Consuming _iter_vcf lazily must not require holding every record in
    memory simultaneously — a basic smoke test that the generator yields
    incrementally rather than building a list under the hood."""
    from pipeline.annotation.stage import _iter_vcf

    vcf = _write_vcf(tmp_path)
    gen = _iter_vcf(vcf)
    first = next(gen)
    assert first.pos == 100
    # generator should still have more to give without having pre-built
    # the full list (can't directly assert memory here cheaply, but at
    # minimum the generator protocol must hold, i.e. this is a generator
    # object, not a list iterator over a pre-built list)
    assert inspect.isgenerator(gen)


def test_streaming_annotation_json_writer_matches_json_dumps(tmp_path):
    from pipeline.annotation.stage import (
        _parse_vcf,
        _write_annotation_json_streaming,
        AnnotationResult,
    )

    vcf = _write_vcf(tmp_path)
    variants = _parse_vcf(vcf)
    result = AnnotationResult(
        sample_id="s1",
        annotated_vcf_path=vcf,
        annotation_json_path=str(tmp_path / "ann.json"),
        total_variants=len(variants),
        annotated_count=0,
        unannotated_count=len(variants),
        gff3_source="",
        elapsed_seconds=1.23,
        variants=variants,
    )
    out_stream = tmp_path / "stream.json"
    out_dumps = tmp_path / "dumps.json"
    _write_annotation_json_streaming(str(out_stream), result)
    out_dumps.write_text(json.dumps(result.to_dict(), indent=2))

    assert json.loads(out_stream.read_text()) == json.loads(out_dumps.read_text())


def test_annotation_stage_run_uses_iter_vcf_not_full_list_upfront():
    """AnnotationStage.run() must consume _iter_vcf() directly rather than
    calling the list-materializing _parse_vcf() before processing."""
    import inspect as _inspect
    from pipeline.annotation import stage as stage_mod

    src = _inspect.getsource(stage_mod.AnnotationStage.run)
    assert "_iter_vcf(filtered_vcf_path" in src
    assert "_parse_vcf(filtered_vcf_path)" not in src
