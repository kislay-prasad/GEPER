"""
SpliceBERT plugin -- real integration (not a placeholder).

--------------------------------------------------------------------
License verification summary (same convention as SpliceFormerPlugin/
EnformerPlugin/BorzoiPlugin -- repeated here so this file is
self-contained):

  - Official implementation: github.com/biomed-AI/SpliceBERT, the
    repository the peer-reviewed paper's own code links to (Chen et
    al., "Self-supervised learning on millions of primary RNA
    sequences from 72 vertebrates improves sequence-based RNA
    splicing prediction", Briefings in Bioinformatics 25(3), bbae163
    (2024), https://doi.org/10.1093/bib/bbae163).
  - Code license: BSD-3-Clause, confirmed by reading the repository's
    own `LICENSE` file directly ("BSD 3-Clause License / Copyright
    (c) 2023, Ken Chen") -- permissive, commercially usable, requires
    only copyright-notice/disclaimer preservation (no attribution-in-
    output requirement, unlike the weights below).
  - Weights license: UNLIKE SpliceFormer's Zenodo archive (where the
    CC-BY-4.0 tag is only the *deposit's* metadata license and the
    code's own MIT LICENSE governs actual use -- see
    spliceformer_plugin.py's docstring), SpliceBERT's pretrained
    weights are published ONLY on Zenodo (DOI 10.5281/zenodo.7995778,
    `models.tar.gz`; the GitHub repo has no weight files of its own,
    its `download.sh` just fetches this same Zenodo URL). Zenodo is
    therefore the actual, sole distribution channel for the weights,
    not merely an archival mirror of something already licensed
    elsewhere -- so that record's own declared license (confirmed via
    Zenodo's own API: `GET /api/records/7995778` ->
    `metadata.license.id == "cc-by-4.0"`) is the operative license for
    the weight files themselves. CC-BY-4.0 is commercially usable and
    redistributable, but -- unlike the BSD-3-Clause code above --
    DOES require attribution to be preserved wherever the weights (or
    predictions derived from them) are redistributed; this metadata
    (`license_notes` below, surfaced via `ModelMetadata`) IS that
    attribution, carried through to any report/audit reader exactly
    the way `ModelMetadata` is designed to be consulted.
  - Net result: both code and weights are commercially usable. No
    copyleft concern (unlike OpenSpliceAI, GPL-3.0); the one real
    difference from SpliceFormer/Enformer/Borzoi's weight licenses is
    CC-BY-4.0's attribution requirement, which is why `license_notes`
    spells it out explicitly rather than reusing "MIT/Apache-2.0,
    notice-preservation only" boilerplate.

Implementation note (why this plugin looks different from
SpliceFormer's vendored-source pattern): SpliceBERT's official
checkpoints are plain HuggingFace `BertForMaskedLM` weights (confirmed
by inspecting the downloaded checkpoint's own `config.json`:
`"architectures": ["BertForMaskedLM"]`, standard `BertTokenizer`
vocab) -- no custom model-definition source exists upstream to vendor
at all. `pipeline/models/splicebert/loader.py` only fetches and
unpacks the official Zenodo archive; the model/tokenizer classes come
directly from the `transformers` package GEPER already depends on
unconditionally (requirements.txt), the same `from_pretrained(...)`
one-liner shape EnformerPlugin/BorzoiPlugin already use for their own
HuggingFace-hosted weights.

Variant-scoring method note: SpliceBERT is a masked-language-model
(MLM) encoder -- it was never given a dedicated splice-site
classification head by its authors (no fine-tuned donor/acceptor
checkpoint is published; the repo's own `examples/04-splicesite-
prediction/` folder *trains* one from labeled data, it doesn't ship
one). This plugin therefore scores a variant the same documented,
zero-shot way the official repo's own downstream analysis notebook
does (`examples/01-variant/variant_analysis.ipynb`, which computes a
`prob_change_score` = the model's masked-position probability of the
reference base minus its probability of the alternate base): mask
each position where the centered ref/alt windows differ, run one
batched forward pass, and read P(ref base) - P(alt base) from the MLM
head's own softmax at that position ("masked-marginal" scoring, the
same zero-shot variant-effect-scoring method established for protein
language models by Meier et al., "Language models enable zero-shot
prediction of the effects of mutations on protein function", NeurIPS
2021). This is a different summary statistic from SpliceFormer's own
acceptor/donor-specific delta (SpliceFormer's checkpoint IS a
dedicated donor/acceptor classifier; SpliceBERT's is not), but is
reported through the exact same `score`/`classification`/
`confidence`/`details`/`meta` shape every other plugin in this family
uses, so callers do not need to special-case it.

Ensemble/evidence-aggregation note: like SpliceFormer, this plugin is
deliberately NOT added to `pipeline/models/ensemble.py`'s
Enformer+Borzoi consensus, not passed to
`InterpretationEngine`/ACMG PP3-PP4 evaluation, not added to
`pipeline/models/status.py`'s "AI Models" display table, and not
wired into `pipeline/orchestrator.py`'s per-variant stages -- see
`spliceformer_plugin.py`'s own module docstring for the identical
reasoning (an unvalidated zero-shot score should not silently become
clinical evidence). It is reachable via `ModelManager` (through
`pipeline.models.pending_plugins.build_default_registry()`) for
direct/programmatic use and tests, exactly like SpliceFormer.
--------------------------------------------------------------------
"""

from typing import Any, Dict, List

import torch

from config import CONFIG
from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.cache import WeightCache
from pipeline.models.splicebert import loader as splicebert_loader
from utils.auto_install import is_pip_package_installed

# Model's fixed position-embedding budget minus [CLS]/[SEP]: the
# longest ref/alt window this checkpoint can score in one pass. See
# pipeline/models/splicebert/loader.py for the config.json sourcing.
SPLICEBERT_MAX_CONTENT_LENGTH = splicebert_loader.MAX_CONTENT_LENGTH

# Same classification thresholds pipeline/models/spliceformer_plugin.py,
# enformer_plugin.py, and borzoi_plugin.py already use for their own
# single-model delta-score summaries -- reused here so a SpliceBERT
# score sits on the same documented 0..~1 scale as the other
# splicing/regulatory plugins' `score`/`classification` fields.
_NO_EFFECT_THRESHOLD = 0.1
_MODERATE_EFFECT_THRESHOLD = 0.5

# Upper bound on how many differing ref/alt positions get their own
# masked forward pass. A SNV differs at exactly one position (the
# common case, scored exactly); a longer indel can differ at many
# positions once both windows are independently centered/padded (the
# same "not perfectly nucleotide-aligned beyond the immediate vicinity
# of the variant" accepted simplification SpliceFormerPlugin/
# EnformerPlugin's own `_prepare_sequence` already documents) -- this
# cap keeps that case's cost bounded (one batched forward pass either
# way) while still scoring the positions closest to the variant,
# which is where the biologically meaningful signal is.
_MAX_SCORED_POSITIONS = 32

_NETWORK_ERROR_TYPES = (OSError, ConnectionError, TimeoutError)

_COMPLEMENT = str.maketrans("ACGTN", "TGCAN")


class SpliceBERTPlugin(PluginModel):
    """Real SpliceBERT integration. Disabled by default
    (`CONFIG.splicing.ENABLE_SPLICEBERT`); once enabled, this loads
    the official pretrained `SpliceBERT.1024nt` checkpoint (BSD-3-
    Clause code / CC-BY-4.0 weights) from the upstream Zenodo archive
    (`pipeline/models/splicebert/loader.py`) via `transformers`'
    standard `BertForMaskedLM`/`BertTokenizer`."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="splicebert",
            version=(
                f"splicebert;zenodo_record={CONFIG.splicing.SPLICEBERT_ZENODO_RECORD};"
                f"checkpoint={CONFIG.splicing.SPLICEBERT_CHECKPOINT}"
            ),
            source="https://github.com/biomed-AI/SpliceBERT",
            license_name="BSD-3-Clause (code) / CC-BY-4.0 (Zenodo-hosted pretrained weights)",
            license_url="https://github.com/biomed-AI/SpliceBERT/blob/main/LICENSE",
            commercial_use_allowed=True,
            license_notes=(
                "Verified against primary sources: the repository's own LICENSE "
                "file (BSD-3-Clause, (c) 2023 Ken Chen) and Zenodo's own record "
                "API (DOI 10.5281/zenodo.7995778, metadata.license.id == "
                "'cc-by-4.0'). Zenodo is the sole distribution channel for the "
                "weights (no GitHub release asset exists), so unlike "
                "SpliceFormer's archive-level CC-BY-4.0 tag (which is superseded "
                "by that project's own MIT code LICENSE -- see "
                "spliceformer_plugin.py), CC-BY-4.0 genuinely governs these "
                "weights and requires attribution to be preserved wherever they "
                "(or predictions derived from them) are redistributed -- this "
                "note IS that attribution. GEPER downloads one official "
                "checkpoint (SpliceBERT.1024nt, the all-vertebrate/variable-"
                "length flagship of the three bundled in the archive) via "
                "pipeline/models/splicebert/loader.py; no model source is "
                "vendored (the checkpoint is a standard HuggingFace "
                "BertForMaskedLM, loaded via transformers' own from_pretrained)."
            ),
        )

    @classmethod
    def is_available(cls) -> bool:
        # `transformers` is already an unconditional GEPER dependency
        # (requirements.txt, used directly by models/esm2.py and
        # others without an auto-install gate) -- not a plugin-
        # specific optional extra the way einops/enformer-pytorch/
        # borzoi-pytorch are for SpliceFormer/Enformer/Borzoi, so this
        # checks importability rather than attempting to install it.
        if not CONFIG.splicing.ENABLE_SPLICEBERT:
            return False
        return is_pip_package_installed("transformers")

    @classmethod
    def unavailability_reason(cls) -> str:
        if not CONFIG.splicing.ENABLE_SPLICEBERT:
            return (
                "disabled via CONFIG.splicing.ENABLE_SPLICEBERT "
                "(set GEPER_ENABLE_SPLICEBERT=true to enable)"
            )
        return "the 'transformers' package is not installed in this environment"

    def __init__(self):
        super().__init__()
        self._weight_cache = WeightCache()
        self.tokenizer = None

    def _load_impl(self) -> None:
        if not is_pip_package_installed("transformers"):
            raise RuntimeError(
                "'transformers' is not installed in this environment (install "
                "it with `pip install -r requirements.txt`)."
            )

        cache_dir = self._weight_cache.ensure_dir("splicebert")
        checkpoint = CONFIG.splicing.SPLICEBERT_CHECKPOINT
        record = CONFIG.splicing.SPLICEBERT_ZENODO_RECORD

        if not splicebert_loader.is_checkpoint_cached(cache_dir, checkpoint):
            try:
                splicebert_loader.download_and_extract_checkpoint(
                    cache_dir, checkpoint=checkpoint, record=record
                )
            except _NETWORK_ERROR_TYPES as exc:
                self.logger.debug(
                    f"SpliceBERT archive fetch for checkpoint '{checkpoint}' "
                    f"failed ({exc.__class__.__name__}): {exc}",
                    exc_info=True,
                )
                raise RuntimeError("SpliceBERT model unavailable") from exc

        checkpoint_dir = splicebert_loader.checkpoint_dir_for(cache_dir, checkpoint)
        try:
            model, tokenizer = splicebert_loader.build_model_and_tokenizer(checkpoint_dir)
        except TimeoutError as exc:
            # `build_model_and_tokenizer` bounds the load itself (see
            # its own docstring for the multi-hour hang this replaces)
            # -- caught here, same as the download failure above, so a
            # load that times out demotes this model to unavailable
            # for the rest of the run instead of propagating a raw
            # timeout message.
            self.logger.debug(f"SpliceBERT checkpoint load for '{checkpoint}' timed out: {exc}", exc_info=True)
            raise RuntimeError("SpliceBERT model unavailable") from exc

        model.to(self.device)
        model.eval()
        self.model = model
        self.tokenizer = tokenizer

    @staticmethod
    def _reverse_complement(sequence: str) -> str:
        return sequence.upper().translate(_COMPLEMENT)[::-1]

    @staticmethod
    def _prepare_sequence(sequence: str, target_length: int = SPLICEBERT_MAX_CONTENT_LENGTH) -> str:
        """Centers `sequence` within a window of `target_length`,
        padding with 'N' (a real vocabulary token for this model,
        unlike SpliceFormer's all-zero one-hot row -- but the same
        "ambiguous/out-of-bounds base" role) or truncating
        symmetrically if it's already longer -- identical shape to
        SpliceFormerPlugin._prepare_sequence, reused here for
        consistency across the plugin family's shared input contract."""
        sequence = sequence.upper().replace("U", "T")
        if len(sequence) == target_length:
            return sequence
        if len(sequence) > target_length:
            excess = len(sequence) - target_length
            start = excess // 2
            return sequence[start : start + target_length]
        pad_total = target_length - len(sequence)
        pad_left = pad_total // 2
        pad_right = pad_total - pad_left
        return ("N" * pad_left) + sequence + ("N" * pad_right)

    def _differing_positions(self, ref_prepared: str, alt_prepared: str) -> List[int]:
        """Positions where the two independently-centered/padded
        windows differ, closest-to-center first, capped at
        `_MAX_SCORED_POSITIONS` -- see that constant's docstring."""
        length = len(ref_prepared)
        center = length // 2
        diffs = [i for i in range(length) if ref_prepared[i] != alt_prepared[i]]
        diffs.sort(key=lambda i: abs(i - center))
        return diffs[:_MAX_SCORED_POSITIONS]

    def _infer_impl(self, ref_seq: str, alt_seq: str, **kwargs) -> Dict[str, Any]:
        """
        Runs a zero-shot, masked-marginal variant scan (see this
        module's docstring for the method and why: SpliceBERT ships no
        fine-tuned splice-site head) and summarizes the predicted
        masked-probability change between the reference and alternate
        allele at each position the two centered windows differ.

        IMPORTANT CALIBRATION CAVEAT: `score`/`classification`/
        `confidence` are a straightforward, documented summary of
        SpliceBERT's raw masked-marginal probability-change signal --
        NOT a clinically calibrated score, and NOT donor/acceptor-
        specific the way SpliceFormer's output is (SpliceBERT has no
        splice-site classification head) -- the same caveat already
        established for every other plugin's own `_infer_impl`.
        """
        strand = kwargs.get("strand", "+")
        if strand == "-":
            ref_seq = self._reverse_complement(ref_seq)
            alt_seq = self._reverse_complement(alt_seq)

        ref_prepared = self._prepare_sequence(ref_seq)
        alt_prepared = self._prepare_sequence(alt_seq)

        positions = self._differing_positions(ref_prepared, alt_prepared)
        total_differing = sum(1 for i in range(len(ref_prepared)) if ref_prepared[i] != alt_prepared[i])

        if not positions:
            return {
                "score": 0.0,
                "classification": "no_significant_effect",
                "confidence": 0.0,
                "details": {
                    "strand": strand,
                    "num_scored_positions": 0,
                    "num_total_differing_positions": 0,
                    "prob_change_scores": [],
                    "calibration_status": (
                        "uncalibrated -- raw SpliceBERT masked-marginal "
                        "probability-change summary, not validated against "
                        "clinical ground truth"
                    ),
                },
            }

        mask_id = self.tokenizer.mask_token_id
        ref_tokenized = " ".join(list(ref_prepared))
        base_input_ids = self.tokenizer.encode(ref_tokenized, return_tensors="pt")[0]

        batch = base_input_ids.unsqueeze(0).repeat(len(positions), 1).clone()
        token_offset = 1  # [CLS] occupies index 0
        for row, pos in enumerate(positions):
            batch[row, pos + token_offset] = mask_id
        batch = batch.to(self.device)

        self.logger.debug(
            "SpliceBERT inference device check -- "
            f"model device: {self.device}, "
            f"input tensor device: {batch.device}, "
            f"input tensor shape: {tuple(batch.shape)}, "
            f"scored positions: {len(positions)} (of {total_differing} differing)"
        )

        with torch.no_grad():
            logits = self.model(input_ids=batch).logits  # (num_positions, seq_len, vocab_size)

        prob_changes = []
        for row, pos in enumerate(positions):
            token_probs = torch.softmax(logits[row, pos + token_offset], dim=-1)
            ref_token_id = self.tokenizer.convert_tokens_to_ids(ref_prepared[pos])
            alt_token_id = self.tokenizer.convert_tokens_to_ids(alt_prepared[pos])
            prob_ref = token_probs[ref_token_id].item()
            prob_alt = token_probs[alt_token_id].item()
            prob_changes.append(prob_ref - prob_alt)

        max_abs_change = max(abs(v) for v in prob_changes)

        if max_abs_change < _NO_EFFECT_THRESHOLD:
            classification = "no_significant_effect"
        elif max_abs_change < _MODERATE_EFFECT_THRESHOLD:
            classification = "moderate_effect"
        else:
            classification = "large_effect"

        # Not a calibrated probability -- see the caveat above. Already
        # bounded to [0, 1] since it's a difference of two softmax
        # probabilities, same convention as the other plugins' scores.
        confidence = min(1.0, max_abs_change)

        return {
            "score": max_abs_change,
            "classification": classification,
            "confidence": confidence,
            "details": {
                "strand": strand,
                "num_scored_positions": len(positions),
                "num_total_differing_positions": total_differing,
                "prob_change_scores": [round(v, 6) for v in prob_changes],
                "checkpoint": CONFIG.splicing.SPLICEBERT_CHECKPOINT,
                "calibration_status": (
                    "uncalibrated -- raw SpliceBERT masked-marginal "
                    "probability-change summary (zero-shot P(ref)-P(alt)); "
                    "not donor/acceptor-specific, not validated against "
                    "clinical ground truth"
                ),
            },
        }
