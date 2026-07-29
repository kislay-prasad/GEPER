"""
SPiP plugin -- real integration (not a placeholder).

--------------------------------------------------------------------
License verification summary (same convention as SpliceFormerPlugin/
SpliceBERTPlugin/EnformerPlugin/BorzoiPlugin -- repeated here so this
file is self-contained):

  - Official implementation: github.com/LBGC-CFB/SPiP (also mirrored
    under github.com/raphaelleman/SPiP, the primary author's own
    account -- same content), the tool described in Leman et al.,
    "SPiP: Splicing Prediction Pipeline, a machine learning based tool
    for splicing prediction", Human Mutation 43(12), 2308-2323 (2022),
    https://doi.org/10.1002/humu.24491.
  - License: MIT, confirmed by reading the repository's own `LICENSE`
    file directly ("MIT License ... Copyright (c) 2020 raphaelleman")
    and by the per-file copyright/permission header
    `SPiPv2.1_main.r` itself carries (Copyright 2019 Center Francois
    Baclesse and Normandie University -- same permissive terms,
    reproduced verbatim in the vendored copy). Covers both the R
    source and its bundled small reference tables
    (`VPP_table.txt`/`VPN_table.txt`).
  - Reference/sequence data (`model.RData`, `RefFiles.RData`,
    `dataRefSeq<genome>.RData`, `transcriptome_<genome>.RData` --
    downloaded separately at load time, see
    `pipeline/models/spip/loader.py`): derived from NCBI RefSeq
    transcript annotation and the hg19/hg38 reference genome
    assemblies, both public-domain US-government/public genome-
    browser data with no additional redistribution restriction --
    distinct from a third party's own proprietary model weights, the
    same category of "reference data", not "someone else's IP", every
    other GEPER stage that reads RefSeq/genome coordinates (VCF
    parsing, BLAST, gnomAD/ClinVar lookups) already relies on.
  - Net result: commercially usable and redistributable (MIT:
    attribution + license notice preserved). No copyleft concern,
    unlike OpenSpliceAI (GPL-3.0; see pipeline/models/pending_plugins.py).

Implementation note (why this plugin looks completely different from
every other plugin in this family): SPiP is not a PyTorch model --
it's an R script (`SPiPv2.1_main.r`) that runs a randomForest
classifier cascading several splicing-prediction components (SPiCE,
MaxEntScan, Branch Point Predictor, ESR/ESE/ESS hexamer scoring),
reimplemented natively in R (confirmed by reading the vendored source
directly: no `system()`/`shell()` calls anywhere, so there's no
*separate* external tool binary to install beyond R itself). GEPER
vendors the official, unmodified R source
(`pipeline/models/spip/vendor/`) and invokes it as a subprocess via
`Rscript`, the same way a user would run it standalone, then parses
its tab-delimited output back into this plugin's usual result shape
-- the same "vendor the real official source, don't reimplement it"
principle SpliceFormerPlugin already applies, extended one level
further (a subprocess boundary instead of an in-process `nn.Module`)
because the official implementation isn't in Python/PyTorch at all.
This mirrors the subprocess-isolation pattern already established
elsewhere in this very codebase for a foreign-language integration --
see `bridge/combined_pipeline.py`'s own module docstring (Kim
pipeline, a separate Python environment) for the same rationale
applied to a different cross-language boundary (there: two Python
processes with incompatible dependency sets; here: Python calling R).

Input-contract note (why `_infer_impl` needs more than ref_seq/alt_seq):
every other plugin in this family scores a fixed-length sequence
*window* (ref vs alt) with no awareness of real exon/intron
boundaries. SPiP is the opposite: its whole value is resolving a
variant's effect against the *actual* annotated transcript structure
(nearest splice site, branch point, reading frame) via a bundled
RefSeq database -- it fundamentally needs genomic coordinates, not
just flanking sequence, to do that lookup. `ref_seq`/`alt_seq` are
still accepted as the first two positional arguments purely for
call-signature consistency with `ModelManager.predict(key, *args,
**kwargs)`'s existing usage pattern across this plugin family; they
are unused. Callers must instead pass `chrom=`, `pos=`, `ref=`,
`alt=` kwargs (the same fields a VCF row carries), which this plugin
writes into a minimal one-variant VCF and hands to SPiP exactly the
way its own VCF input mode works.

Ensemble/evidence-aggregation note: like SpliceFormer/SpliceBERT,
this plugin is deliberately NOT added to `pipeline/models/ensemble.py`'s
Enformer+Borzoi consensus, not passed to `InterpretationEngine`/ACMG
PP3-PP4 evaluation, not added to `pipeline/models/status.py`'s "AI
Models" display table, and not wired into
`pipeline/orchestrator.py`'s per-variant stages -- see
`spliceformer_plugin.py`'s own module docstring for the identical
reasoning. It is reachable via `ModelManager` (through
`pipeline.models.pending_plugins.build_default_registry()`) for
direct/programmatic use and tests, exactly like SpliceFormer/SpliceBERT.
--------------------------------------------------------------------
"""

from typing import Any, Dict, List

from config import CONFIG
from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.cache import WeightCache
from pipeline.models.spip import loader as spip_loader

# Same classification thresholds pipeline/models/spliceformer_plugin.py,
# splicebert_plugin.py, enformer_plugin.py, and borzoi_plugin.py already
# use for their own single-model summaries -- reused here (for the
# moderate/large split only; see _infer_impl's own comment for why
# "no effect" is instead read directly from SPiP's own "NTR"
# interpretation rather than a reapplied 0.1 cutoff) so a SPiP score
# sits on the same documented 0..~1 scale as the rest of this family.
_MODERATE_EFFECT_THRESHOLD = 0.5

_NETWORK_ERROR_TYPES = (OSError, ConnectionError, TimeoutError)


def _parse_spip_output(output_text: str) -> List[Dict[str, str]]:
    """
    SPiP's own tab-delimited output (VCF-input, non---VCF-output mode):
    one header line (the input VCF's own columns, then `varID`,
    `Interpretation`, `InterConfident`, `SPiPscore`, and 31 more
    annotation columns -- see SPiPv2.1_main.r's own `names(...)  <-
    c(...)` assignment for the exact, authoritative list) followed by
    one data line per annotated transcript the variant overlaps (a
    variant can hit more than one transcript/row). Returns a list of
    plain dicts, header-name -> value, one per data row -- no column
    dropped or renamed, so every field SPiP itself reports stays
    inspectable in `details` without this parser having to know which
    ones matter.
    """
    lines = [line for line in output_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        rows.append(dict(zip(header, values)))
    return rows


class SpipPlugin(PluginModel):
    """Real SPiP integration. Disabled by default
    (`CONFIG.splicing.ENABLE_SPIP`); once enabled, this vendors and
    runs the official, unmodified upstream R source
    (`pipeline/models/spip/vendor/`) as a subprocess, with reference
    data (trained randomForest model, RefSeq transcript annotation,
    genome sequence) downloaded from the official repository/its
    linked SourceForge host (`pipeline/models/spip/loader.py`)."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="spip",
            version=f"spip;genome={CONFIG.splicing.SPIP_GENOME_ASSEMBLY}",
            source="https://github.com/LBGC-CFB/SPiP",
            license_name="MIT",
            license_url="https://github.com/LBGC-CFB/SPiP/blob/master/LICENSE",
            commercial_use_allowed=True,
            license_notes=(
                "Verified against the primary source: the repository's own "
                "LICENSE file (MIT, (c) 2020 raphaelleman) and the same "
                "permissive header reproduced in SPiPv2.1_main.r itself. "
                "GEPER vendors the official, unmodified R source "
                "(pipeline/models/spip/vendor/) and downloads reference/"
                "sequence data (trained model, RefSeq transcript annotation, "
                "genome sequence) from the official repository and its own "
                "linked SourceForge host -- see "
                "pipeline/models/spip/loader.py. That data is NCBI RefSeq/"
                "hg19-hg38 reference-genome data, not a third party's "
                "proprietary model weights -- public domain / unrestricted "
                "for redistribution, the same category of reference data "
                "every other GEPER stage reading genomic coordinates already "
                "relies on."
            ),
        )

    @classmethod
    def is_available(cls) -> bool:
        if not CONFIG.splicing.ENABLE_SPIP:
            return False
        return spip_loader.find_rscript() is not None

    @classmethod
    def unavailability_reason(cls) -> str:
        if not CONFIG.splicing.ENABLE_SPIP:
            return (
                "disabled via CONFIG.splicing.ENABLE_SPIP "
                "(set GEPER_ENABLE_SPIP=true to enable)"
            )
        return (
            "no 'Rscript' executable found in this environment "
            "(install R: https://cran.r-project.org/)"
        )

    def __init__(self):
        super().__init__()
        self._weight_cache = WeightCache()
        self._rscript_path = None
        self._runtime_dir = None
        self._genome = None

    def _load_impl(self) -> None:
        rscript_path = spip_loader.find_rscript()
        if not rscript_path:
            raise RuntimeError(
                "No 'Rscript' executable found (install R: "
                "https://cran.r-project.org/)."
            )
        if not spip_loader.ensure_r_packages(rscript_path):
            raise RuntimeError(
                f"Automatic installation of required R package(s) "
                f"({spip_loader.REQUIRED_R_PACKAGES}) did not succeed in "
                "this environment (check network access to cloud.r-project.org, "
                "or install them yourself from an R console with "
                "`install.packages(c(\"foreach\",\"doParallel\",\"randomForest\"))`)."
            )

        cache_dir = self._weight_cache.ensure_dir("spip")
        genome = CONFIG.splicing.SPIP_GENOME_ASSEMBLY

        try:
            runtime_dir = spip_loader.prepare_runtime_dir(cache_dir, genome=genome)
        except _NETWORK_ERROR_TYPES as exc:
            self.logger.debug(
                f"SPiP reference-data fetch for genome '{genome}' failed "
                f"({exc.__class__.__name__}): {exc}",
                exc_info=True,
            )
            raise RuntimeError("SPiP model unavailable") from exc

        self._rscript_path = rscript_path
        self._runtime_dir = runtime_dir
        self._genome = genome
        # No persistent in-memory model object (see this module's
        # docstring: every prediction is a fresh Rscript subprocess) --
        # `self.model` is set to a plain, inspectable config dict
        # purely to satisfy PluginModel's "_load_impl must set
        # self.model" contract.
        self.model = {"rscript": rscript_path, "runtime_dir": str(runtime_dir), "genome": genome}

    def _infer_impl(self, ref_seq: str, alt_seq: str, **kwargs) -> Dict[str, Any]:
        """
        See this module's docstring's "Input-contract note" -- SPiP
        needs genomic coordinates (`chrom=`, `pos=`, `ref=`, `alt=`
        kwargs), not `ref_seq`/`alt_seq` (accepted but unused, for
        call-signature consistency only). Writes a one-variant VCF,
        runs the vendored SPiP subprocess against it, and summarizes
        its own `SPiPscore` (a calibrated randomForest probability,
        0..1 -- unlike every other plugin in this family, whose scores
        are explicitly documented as *uncalibrated*) and
        `Interpretation` (SPiP's own categorical verdict, e.g. "NTR"
        for no transcript repercussion, or "Alter by SPiCE"/"Alter by
        complex event"/etc. for a predicted splicing effect).

        A variant may overlap more than one annotated transcript; all
        of SPiP's per-transcript rows are kept in `details["all_rows"]`,
        and the primary summary (`score`/`classification`/`details`'
        top-level fields) uses the row matching `gene=` if given, else
        the first row SPiP returned.
        """
        chrom = kwargs.get("chrom")
        pos = kwargs.get("pos")
        ref = kwargs.get("ref")
        alt = kwargs.get("alt")
        if chrom is None or pos is None or ref is None or alt is None:
            raise ValueError(
                "SpipPlugin requires genomic coordinates, not a bare "
                "ref_seq/alt_seq window: pass chrom=, pos=, ref=, alt= "
                "kwargs (the same fields one VCF row carries). SPiP "
                "resolves the affected transcript(s)/exon-intron "
                "structure itself from its own bundled RefSeq annotation "
                "-- see this plugin module's docstring."
            )
        genome = kwargs.get("genome", self._genome or CONFIG.splicing.SPIP_GENOME_ASSEMBLY)
        gene = kwargs.get("gene")

        output_text = spip_loader.run_spip(
            self._rscript_path,
            self._runtime_dir,
            chrom=str(chrom),
            pos=int(pos),
            ref=str(ref),
            alt=str(alt),
            genome=genome,
            timeout=CONFIG.splicing.SPIP_TIMEOUT_SECONDS,
        )
        rows = _parse_spip_output(output_text)
        if not rows:
            raise RuntimeError(
                "SPiP produced no output rows for this variant (check that "
                f"chrom={chrom!r} pos={pos!r} ref={ref!r} alt={alt!r} fall "
                f"within a RefSeq-annotated transcript for genome={genome!r})."
            )

        matching = [r for r in rows if gene and r.get("gene") == gene]
        primary = matching[0] if matching else rows[0]

        try:
            raw_score = float(primary.get("SPiPscore", "-1"))
        except ValueError:
            raw_score = -1.0
        interpretation = primary.get("Interpretation", "")

        # SPiPscore is a calibrated randomForest probability in [0,1];
        # -1 is SPiP's own sentinel for "could not be scored" (e.g. an
        # unsupported variant type) -- clamp defensively so a bad
        # sentinel never produces a negative "score" for downstream
        # consumers expecting the same non-negative-magnitude
        # convention every other plugin in this family uses.
        score = max(0.0, raw_score)

        # "NTR" ("no transcript repercussion") is SPiP's own
        # region-aware verdict (it already applies different internal
        # thresholds for exonic vs intronic positions -- see
        # SPiPv2.1_main.r's thToSPiPexon/thToSPiPintron), a more
        # accurate "no effect" signal here than reapplying this
        # family's own generic 0.1 cutoff would be. The moderate/large
        # split below still reuses the shared family threshold so
        # SPiP's classification bucket sits on the same scale as
        # SpliceFormer/SpliceBERT/Enformer/Borzoi's own.
        if interpretation == "NTR" or raw_score < 0:
            classification = "no_significant_effect"
        elif score < _MODERATE_EFFECT_THRESHOLD:
            classification = "moderate_effect"
        else:
            classification = "large_effect"

        return {
            "score": score,
            "classification": classification,
            "confidence": min(1.0, score),
            "details": {
                "interpretation": interpretation,
                "inter_confident": primary.get("InterConfident", ""),
                "spip_score": raw_score,
                "region_type": primary.get("RegType", ""),
                "spice_proba": primary.get("SPiCEproba", ""),
                "delta_mes": primary.get("deltaMES", ""),
                "delta_esr_score": primary.get("deltaESRscore", ""),
                "nearest_ss_distance": primary.get("DistSS", ""),
                "gene": primary.get("gene", ""),
                "transcript": primary.get("transcript", ""),
                "nt_change": primary.get("ntChange", ""),
                "num_transcripts_matched": len(rows),
                "all_rows": rows,
                "genome": genome,
                "calibration_status": (
                    "SPiPscore is SPiP's own calibrated randomForest "
                    "probability (trained/validated against a curated "
                    "splicing-outcome dataset in the original publication) "
                    "-- unlike every other plugin in this family, which is "
                    "explicitly uncalibrated. Still not independently "
                    "re-validated by GEPER against its own clinical ground "
                    "truth."
                ),
            },
        }
