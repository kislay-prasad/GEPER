"""
tests/test_pdf_report.py
──────────────────────────
Tests for pipeline/reporting/pdf_report.py — the ReportLab-native
clinical PDF generator (Issue 3).
"""

from __future__ import annotations

from pipeline.reporting.pdf_report import render_clinical_pdf, ReportLabUnavailableError
from pipeline.reporting.stage import PIPELINE_VERSION
from pipeline.reporting.clinical_sections import (
    normalize_patient_metadata,
    qc_status_summary,
    variant_dashboard,
    clinical_interpretation,
    merge_variants_with_acmg,
)


def _default_disclaimer():
    from pipeline.reporting.component_identity import RESEARCH_USE_DISCLAIMER

    return RESEARCH_USE_DISCLAIMER


def _flatten(text):
    """Collapse the line breaks a wrapped PDF footer introduces, so a
    multi-line rendering can be compared against the single-line source
    string. Whitespace only -- no words are altered."""
    return " ".join(text.split())


def _minimal_kwargs():
    patient = normalize_patient_metadata(None)
    qc_rows = qc_status_summary({"q30_fraction": 0.9}, {"mean_depth": 30, "pct_mapped": 98})
    dashboard = variant_dashboard([{"classification": "Uncertain_Significance"}], {})
    interpretation = clinical_interpretation(dashboard)
    merged = merge_variants_with_acmg(
        [
            {
                "chrom": "MT",
                "pos": 3243,
                "ref": "A",
                "alt": "G",
                "gene_name": "MT-TL1",
                "transcript_id": "rna-TRNL1",
                "hgvs": "MT:g.3243A>G",
                "consequence": "gene_region_variant",
            }
        ],
        [
            {
                "chrom": "MT",
                "pos": 3243,
                "ref": "A",
                "alt": "G",
                "gene": "MT-TL1",
                "classification": "Uncertain_Significance",
                "gnomad_af": 0.0001,
            }
        ],
    )
    return dict(
        patient_meta=patient,
        qc_rows=qc_rows,
        dashboard=dashboard,
        interpretation=interpretation,
        merged_variants=merged,
        reference_genome="GRCh38",
        pipeline_version=PIPELINE_VERSION,
    )


class TestRenderClinicalPdf:
    def test_produces_a_valid_pdf_file(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        result_path = render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        assert result_path == out
        reader = PdfReader(out)
        assert len(reader.pages) >= 1

    def test_patient_metadata_appears_in_pdf_text(self, tmp_path):
        from pypdf import PdfReader

        kwargs = _minimal_kwargs()
        kwargs["patient_meta"] = normalize_patient_metadata(
            {"name": "Jane Doe", "physician": "Dr. Smith"}
        )
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **kwargs)
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "Jane Doe" in text
        assert "Dr. Smith" in text

    def test_deidentified_note_appears_when_no_patient_metadata(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "de-identified" in text.lower()

    def test_variant_gene_and_classification_appear(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "MT-TL1" in text
        assert "Uncertain_Significance" in text

    def test_footer_contains_reference_genome_and_pipeline_version(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "GRCh38" in text
        # Was "Bij AI v8" -- the whole-product name, superseded 2026-09-11
        # (Option B): the version names the component, as the scope line does.
        assert "Bij AI sequencing-analysis component v8" in text

    def test_lab_disclaimer_present(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        # The footer intentionally truncates a long disclaimer to fit one
        # line — check the start of the default disclaimer, which is
        # guaranteed to survive truncation.
        #
        # UPDATED 2026-08-22 (EJ-01): pipeline/reporting/pdf_report.py's
        # default `lab_disclaimer` text was rewritten that day to the
        # ratified assisted-clinical-use wording, superseding the
        # research/orthogonal-testing framing it previously asserted (see
        # that module's own comment). This specific assertion needed no
        # change: "in-development bioinformatics pipeline" was
        # deliberately kept, verbatim, as the new text's own opening
        # clause, so it still survives the 150-character footer
        # truncation unchanged. Noted here rather than left silent, so
        # this unchanged assertion doesn't read as having been missed.
        #
        # UPDATED again (EJ-01 alignment, human ruling "Option 1"): the
        # default is now component_identity.RESEARCH_USE_DISCLAIMER, which is
        # GEPER's text byte for byte. The footer no longer truncates it at
        # all -- see TestFooterDisclaimerIsWhole below, which owns that claim
        # in full. This test keeps its narrower job: the DEFAULT is used when
        # no lab_disclaimer is passed.
        from pipeline.reporting.component_identity import RESEARCH_USE_DISCLAIMER

        assert _flatten(RESEARCH_USE_DISCLAIMER) in _flatten(text)

    def test_footer_disclaimer_is_not_cut_off_mid_word(self, tmp_path):
        """The regression this class exists for, stated as a single check:
        the footer used to draw `lab_disclaimer[:150]`, which ended the
        sentence at "...machine-learning predict"."""
        from pypdf import PdfReader

        from pipeline.reporting.component_identity import RESEARCH_USE_DISCLAIMER

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = _flatten("".join(p.extract_text() for p in PdfReader(out).pages))
        truncated = _flatten(RESEARCH_USE_DISCLAIMER[:150])  # "...machine-learning predict"
        assert truncated.endswith("machine-learning predict"), "the 150-char cut point moved"
        assert not text.split(truncated)[1].startswith(" Reference genome"), (
            "the footer disclaimer is still cut off at 150 characters"
        )
        assert "machine-learning predictors." in text

    def test_custom_disclaimer_used_when_provided(self, tmp_path):
        from pypdf import PdfReader

        kwargs = _minimal_kwargs()
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", lab_disclaimer="CUSTOM DISCLAIMER TEXT", **kwargs)
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "CUSTOM DISCLAIMER TEXT" in text

    def test_signature_block_not_rendered(self, tmp_path):
        """Inverted, not deleted, from the original `assert "Pathologist"
        in text`: that assertion encoded a claim -- "this report carries
        a lab sign-off block" -- that the product now rules false, rather
        than documenting a held defect. This is the PDF twin of the same
        signature-block/signature-line pair removed from the HTML report
        (see test_reporting_stage.py::test_signature_block_not_rendered
        for the full reasoning: no review_status field, no
        approve()/override(), no export gate anywhere in kim_pipeline --
        a control indistinguishable from no control is worse than no
        control, because it launders the absence of one). The block
        itself was removed from pipeline/reporting/pdf_report.py in
        cfba75e; this test was missed by that commit's authorization
        (named only pdf_report.py and one other test file) because it
        lives in a second test file pinning the same artefact. If this
        assertion starts failing, that means the block came back -- do
        not "fix" it by reverting to `in text`.
        """
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "Pathologist" not in text

    def test_page_x_of_y_pagination(self, tmp_path):
        """Force multiple pages via a long variant list and confirm each
        page's footer states the correct total."""
        from pypdf import PdfReader

        kwargs = _minimal_kwargs()
        many_variants = [
            {
                "chrom": "17",
                "pos": i,
                "ref": "A",
                "alt": "T",
                "gene_name": f"GENE{i}",
                "transcript_id": f"NM_{i}",
                "hgvs": f"g.{i}A>T",
                "consequence": "missense_variant",
            }
            for i in range(200)
        ]
        many_acmg = [
            {
                "chrom": "17",
                "pos": i,
                "ref": "A",
                "alt": "T",
                "gene": f"GENE{i}",
                "classification": "Uncertain_Significance",
                "gnomad_af": 0.001,
            }
            for i in range(200)
        ]
        kwargs["merged_variants"] = merge_variants_with_acmg(many_variants, many_acmg)
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **kwargs)
        reader = PdfReader(out)
        n = len(reader.pages)
        assert n > 1
        first_text = reader.pages[0].extract_text()
        last_text = reader.pages[-1].extract_text()
        assert f"Page 1 of {n}" in first_text
        assert f"Page {n} of {n}" in last_text

    def test_no_variants_does_not_crash(self, tmp_path):
        kwargs = _minimal_kwargs()
        kwargs["merged_variants"] = []
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **kwargs)  # must not raise

    def test_reportlab_unavailable_raises_specific_error(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "reportlab.lib" or name.startswith("reportlab"):
                raise ImportError("simulated missing reportlab")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        out = str(tmp_path / "report.pdf")
        try:
            render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
            assert False, "expected ReportLabUnavailableError"
        except ReportLabUnavailableError:
            pass


# ── the footer prints the WHOLE disclaimer, on every page ────────────────────
#
# REGRESSION THIS CLASS EXISTS FOR: the footer drew `lab_disclaimer[:150]` on
# one line. That truncated nothing while Kim's own shorter text was in place,
# but once Kim adopted GEPER's ratified text (EJ-01, human ruling "Option 1")
# it cut the sentence mid-word at "...machine-learning predict" -- losing the
# operative clause the human ruled the point of the disclaimer: "not a
# substitute for professional clinical genetic interpretation, diagnosis, or
# advice". A disclaimer that stops before its own operative sentence is worse
# than the shorter text it replaced, so the footer now wraps instead.


def _render_and_capture_frame_bottom(pdf_path, **kwargs):
    """Render for real and report the frame bottom the document was built
    with, captured from the live footer callback. That value IS the layout
    contract: ReportLab lays every content flowable out above the frame
    bottom, so `footer top <= frame bottom` means no collision.

    (Read from the build rather than from pypdf on purpose: pypdf's per-run
    text matrices do not compose reliably for platypus content -- two
    consecutive lines of one paragraph report baselines 123pt apart -- so
    extracted coordinates cannot prove or disprove an overlap here.)"""
    import pipeline.reporting.pdf_report as pdf_module

    seen = []
    original = pdf_module._draw_footer_without_page_number

    def spy(canvas, doc_, *args):
        seen.append(doc_.bottomMargin)
        return original(canvas, doc_, *args)

    pdf_module._draw_footer_without_page_number = spy
    try:
        pdf_module.render_clinical_pdf(pdf_path, **kwargs)
    finally:
        pdf_module._draw_footer_without_page_number = original
    assert seen, "the footer callback never ran"
    assert len(set(seen)) == 1, f"frame bottom differed between pages: {sorted(set(seen))}"
    return seen[0]


class TestFooterDisclaimerIsWhole:
    def test_every_page_footer_carries_the_disclaimer_verbatim_to_its_last_word(self, tmp_path):
        from pypdf import PdfReader

        from pipeline.reporting.component_identity import RESEARCH_USE_DISCLAIMER

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        pages = PdfReader(out).pages
        assert len(pages) > 1, "this fixture is expected to span more than one page"
        for n, page in enumerate(pages, start=1):
            flat = _flatten(page.extract_text())
            assert RESEARCH_USE_DISCLAIMER in flat, f"page {n} lost part of the disclaimer"
            # The operative clause, named explicitly so a future truncation
            # cannot pass by keeping merely "most" of the text.
            assert (
                "It is not a substitute for professional clinical genetic interpretation, "
                "diagnosis, or advice." in flat
            ), f"page {n} is missing the operative closing sentence"

    def test_a_custom_disclaimer_of_any_length_is_also_printed_whole(self, tmp_path):
        from pypdf import PdfReader

        custom = (
            "CUSTOM LAB DISCLAIMER. " + "This sentence exists only to make the text long enough "
            "to need several footer lines, so the wrap is exercised by a caller-supplied string "
            "and not only by the default constant. " * 2 + "END OF CUSTOM DISCLAIMER."
        )
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", lab_disclaimer=custom, **_minimal_kwargs())
        flat = _flatten(PdfReader(out).pages[0].extract_text())
        assert _flatten(custom) in flat

    # ── controls: the rest of the footer, the layout, and the page count ──

    def test_the_rest_of_the_footer_still_renders_on_every_page(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        pages = PdfReader(out).pages
        n = len(pages)
        for i, page in enumerate(pages, start=1):
            flat = _flatten(page.extract_text())
            assert "Reference genome: GRCh38" in flat
            assert f"Pipeline: {PIPELINE_VERSION}" in flat
            assert "Generated:" in flat
            assert f"Page {i} of {n}" in flat

    def test_footer_block_does_not_overlap_the_content_above_it(self, tmp_path):
        """The wrapped footer grows upward, so the content frame above it has
        to move up with it. Under the old one-line footer the frame bottom was
        a flat 0.8 inch (57.6pt) that knew nothing about the footer at all."""
        from reportlab.lib.pagesizes import letter

        from pipeline.reporting.pdf_report import footer_block_top, footer_disclaimer_lines

        out = str(tmp_path / "report.pdf")
        frame_bottom = _render_and_capture_frame_bottom(out, sample_id="S01", **_minimal_kwargs())
        lines = footer_disclaimer_lines(_default_disclaimer(), letter[0])
        assert len(lines) > 1, "this disclaimer is expected to need a wrapped footer"
        assert footer_block_top(len(lines)) <= frame_bottom, (
            f"footer top {footer_block_top(len(lines)):.1f}pt reaches into the content "
            f"frame, which starts at {frame_bottom:.1f}pt"
        )
        # And it really did grow: the old flat margin would not have fitted.
        assert frame_bottom > 0.8 * 72.0

    def test_a_short_disclaimer_leaves_the_original_layout_untouched(self, tmp_path):
        """One line still costs exactly what it used to: the 0.8 inch frame
        bottom this report was laid out with before the footer could wrap."""
        out = str(tmp_path / "report.pdf")
        frame_bottom = _render_and_capture_frame_bottom(
            out, sample_id="S01", lab_disclaimer="Short disclaimer.", **_minimal_kwargs()
        )
        assert frame_bottom == 0.8 * 72.0

    def test_page_count_is_unchanged_by_the_taller_footer(self, tmp_path):
        """Measured, both before and after the wrap: this fixture renders 2
        pages. The taller footer costs ~22pt of frame height per page, which
        this report absorbs without spilling onto a third page. If a future
        change moves this number, that is a real layout change and wants a
        deliberate update, not a silent one."""
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        assert len(PdfReader(out).pages) == 2
