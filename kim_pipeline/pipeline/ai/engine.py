"""
pipeline/ai/engine.py
───────────────────────
AI model inference engine — DNABERT-2 (DNA sequence) + ESM-2 (protein).

Two models are supported:

  DNABERT-2 (zhihan1996/DNABERT-2-117M)
    Input:  DNA sequence context window around the variant (k-mer tokenised)
    Output: Pathogenicity score in [0, 1] derived from CLS embedding

  ESM-2 (facebook/esm2_t12_35M_UR50D)
    Input:  Wild-type and mutant amino-acid sequences around the variant
    Output: Embedding distance score in [0, 1] (larger = more disruptive)

Both models require:
  pip install torch transformers

If the libraries are not installed, every public method returns None
gracefully rather than crashing — the AI stream simply contributes 0
weight to the evidence engine (handled by EvidenceAggregator).

GPU / CPU selection:
  cfg["dnabert2"]["device"] and cfg["esm2"]["device"] accept:
    "auto"  — CUDA if available, else CPU
    "cpu"   — force CPU
    "cuda"  — force GPU (raises if not available)

Models are lazy-loaded on first call and cached for the lifetime of the
engine instance. Batch inference is supported.

Usage::

    from pipeline.ai.engine import AiEngine

    engine = AiEngine(cfg={
        "dnabert2": {"model_name": "zhihan1996/DNABERT-2-117M", "device": "auto"},
        "esm2":     {"model_name": "facebook/esm2_t12_35M_UR50D", "device": "auto"},
    })

    dna_score = engine.score_dna(
        ref_sequence="ACGTACGTACGT",
        alt_sequence="ACGTATGTACGT",
    )
    protein_score = engine.score_protein(
        wildtype_aa="MTEYKLVVVGA",
        mutant_aa  ="MTEYKLVVVGV",
    )
    combined = engine.combined_score(dna_score, protein_score)
"""

from __future__ import annotations

import logging
import math
import time

logger = logging.getLogger("geper.pipeline.ai.engine")

# Lazy imports — torch and transformers are optional
_torch = None
_transformers = None


def _try_import_torch():
    global _torch
    if _torch is not None:
        return _torch
    try:
        import torch

        _torch = torch
    except ImportError:
        logger.warning(
            "PyTorch not installed — AI engine disabled. "
            "Install with: pip install torch transformers"
        )
        _torch = False
    return _torch


def _try_import_transformers():
    global _transformers
    if _transformers is not None:
        return _transformers
    try:
        import transformers

        _transformers = transformers
    except ImportError:
        _transformers = False
    return _transformers


_cuda_diag_logged = False


def _log_cuda_diagnostics_once(torch) -> None:
    """Log torch/CUDA build info exactly once per process.

    Diagnostic only — does not change device selection. Exists because
    "CUDA unavailable" can mean several different things (no GPU runtime,
    a CPU-only torch wheel, driver/toolkit mismatch) and the previous
    behavior gave no visibility into which one applied.
    """
    global _cuda_diag_logged
    if _cuda_diag_logged:
        return
    _cuda_diag_logged = True
    try:
        cuda_available = torch.cuda.is_available()
        logger.info(
            "[AI:CUDA_DIAGNOSTICS] torch=%s torch_built_with_cuda=%s "
            "cuda_available=%s device_count=%d",
            getattr(torch, "__version__", "unknown"),
            getattr(torch.version, "cuda", None) is not None,
            cuda_available,
            torch.cuda.device_count() if cuda_available else 0,
        )
        if not cuda_available:
            logger.info(
                "[AI:CUDA_DIAGNOSTICS] CUDA not available — models will run on CPU. "
                "Common causes: (1) Colab runtime not set to a GPU type "
                "(Runtime > Change runtime type), (2) a CPU-only torch build is "
                "installed (torch.version.cuda is None in that case), (3) a later "
                "pip install (e.g. with --no-deps) replaced the CUDA build with a "
                'CPU-only one. Check with: python -c "import torch; '
                'print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"'
            )
    except Exception:
        pass  # diagnostics must never affect scoring


def _resolve_device(device_cfg: str):
    """Resolve 'auto' | 'cpu' | 'cuda' to a torch device string."""
    torch = _try_import_torch()
    if not torch:
        return "cpu"
    _log_cuda_diagnostics_once(torch)
    if device_cfg == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    return device_cfg


# ─── DNABERT-2 engine ─────────────────────────────────────────────────────────

# DNABERT-2 loads via trust_remote_code=True (its architecture — MosaicBERT
# with ALiBi/FlashAttention — ships as custom Python in the HF repo, not in
# the transformers library). Pinning to a reviewed commit closes the
# moving-ref RCE surface: without a revision pin, trust_remote_code=True
# would execute whatever code sits on the repo's default branch at load
# time, which the repo owner (or an attacker who compromises their account)
# could silently repoint to something malicious after this review. Reviewed
# 2026-08-20 (see DATA_PROVENANCE.md) — bert_layers.py / bert_padding.py /
# configuration_bert.py / flash_attn_triton.py at this commit contain only
# standard torch/einops/triton model code: no subprocess, eval/exec, sockets,
# HTTP calls, or dynamic imports.
_DNABERT2_PINNED_REVISION = "7bce263b15377fc15361f52cfab88f8b586abda0"


class DnaBertEngine:
    """DNABERT-2 variant pathogenicity scorer.

    Scores a variant by comparing the CLS-token embedding of the
    reference context window vs the alternate context window. The
    cosine distance between embeddings is mapped to [0, 1] where
    1 = maximally disruptive (pathogenic) and 0 = identical embedding.

    Args:
        model_name: HuggingFace model ID (default: zhihan1996/DNABERT-2-117M).
        device:     "auto" | "cpu" | "cuda".
        batch_size: Inference batch size.
    """

    def __init__(
        self,
        model_name: str = "zhihan1996/DNABERT-2-117M",
        device: str = "auto",
        batch_size: int = 8,
    ) -> None:
        self._model_name = model_name
        self._device_cfg = device
        self._batch_size = batch_size
        self._model = None
        self._tokenizer = None
        self._device = None

    def _load(self) -> bool:
        """Lazy-load model. Returns True on success, False if unavailable."""
        if self._model is not None:
            return True
        torch = _try_import_torch()
        transformers = _try_import_transformers()
        if not torch or not transformers:
            return False
        _t0 = time.monotonic()
        logger.info("[AI:DNABERT-2:LOAD] START model=%s", self._model_name)
        try:
            logger.info("Loading DNABERT-2 model: %s …", self._model_name)
            self._device = _resolve_device(self._device_cfg)
            # Only pin to the reviewed commit when loading the default repo —
            # an operator-supplied model_name (e.g. a local fork) is their
            # own responsibility and a stale pin would just break the load.
            revision = (
                _DNABERT2_PINNED_REVISION
                if self._model_name == "zhihan1996/DNABERT-2-117M"
                else None
            )
            self._tokenizer = transformers.AutoTokenizer.from_pretrained(
                self._model_name, trust_remote_code=True, revision=revision
            )
            self._model = transformers.AutoModel.from_pretrained(
                self._model_name, trust_remote_code=True, revision=revision
            )
            self._model.eval()
            self._model.to(self._device)
            logger.info("DNABERT-2 loaded on %s", self._device)
            logger.info(
                "[AI:DNABERT-2:LOAD] END model=%s device=%s elapsed=%.2fs",
                self._model_name,
                self._device,
                time.monotonic() - _t0,
            )
            return True
        except Exception as exc:
            logger.error("Failed to load DNABERT-2: %s", exc)
            logger.info(
                "[AI:DNABERT-2:LOAD] END (failed) model=%s elapsed=%.2fs",
                self._model_name,
                time.monotonic() - _t0,
            )
            return False

    def _embed(self, sequence: str):
        """Return CLS embedding tensor for a DNA sequence string."""
        torch = _torch
        inputs = self._tokenizer(
            sequence,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512,
        )
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = self._model(**inputs)
        # CLS token = first token of last hidden state
        cls_embedding = outputs.last_hidden_state[:, 0, :]
        return cls_embedding

    def score(self, ref_sequence: str, alt_sequence: str) -> float | None:
        """Return a pathogenicity score in [0, 1] for a variant.

        Compares CLS embeddings of the reference and alternate context
        windows. Uses 1 - cosine_similarity, scaled to [0, 1].

        Args:
            ref_sequence: DNA sequence containing the reference allele.
            alt_sequence: DNA sequence containing the alternate allele.

        Returns:
            Float in [0, 1] or None if model is not available.
        """
        _t0 = time.monotonic()
        logger.info("[AI:DNABERT-2:SCORE] START")
        if not self._load():
            logger.info(
                "[AI:DNABERT-2:SCORE] END (unavailable) elapsed=%.2fs",
                time.monotonic() - _t0,
            )
            return None
        try:
            torch = _torch
            emb_ref = self._embed(ref_sequence)
            emb_alt = self._embed(alt_sequence)
            cos_sim = torch.nn.functional.cosine_similarity(emb_ref, emb_alt).item()
            # cosine_similarity in [-1, 1]; map to pathogenicity [0, 1]
            score = round((1.0 - cos_sim) / 2.0, 4)
            logger.debug("DNABERT-2 score: %.4f", score)
            logger.info("[AI:DNABERT-2:SCORE] END elapsed=%.2fs", time.monotonic() - _t0)
            return score
        except Exception as exc:
            logger.error("DNABERT-2 inference failed: %s", exc)
            logger.info(
                "[AI:DNABERT-2:SCORE] END (error) elapsed=%.2fs",
                time.monotonic() - _t0,
            )
            return None

    def score_batch(self, pairs: list[tuple[str, str]]) -> list[float | None]:
        """Score a batch of (ref_seq, alt_seq) pairs. Returns a list of scores."""
        return [self.score(r, a) for r, a in pairs]


# ─── ESM-2 engine ─────────────────────────────────────────────────────────────


class Esm2Engine:
    """ESM-2 protein variant impact scorer.

    Scores a missense variant by comparing the mean-pooled embeddings
    of the wild-type and mutant amino-acid sequences in a window around
    the substitution position.

    Args:
        model_name: HuggingFace model ID (default: facebook/esm2_t12_35M_UR50D).
        device:     "auto" | "cpu" | "cuda".
        batch_size: Inference batch size.
    """

    def __init__(
        self,
        model_name: str = "facebook/esm2_t12_35M_UR50D",
        device: str = "auto",
        batch_size: int = 4,
    ) -> None:
        self._model_name = model_name
        self._device_cfg = device
        self._batch_size = batch_size
        self._model = None
        self._tokenizer = None
        self._device = None

    def _load(self) -> bool:
        if self._model is not None:
            return True
        torch = _try_import_torch()
        transformers = _try_import_transformers()
        if not torch or not transformers:
            return False
        _t0 = time.monotonic()
        logger.info("[AI:ESM-2:LOAD] START model=%s", self._model_name)
        try:
            logger.info("Loading ESM-2 model: %s …", self._model_name)
            self._device = _resolve_device(self._device_cfg)
            self._tokenizer = transformers.AutoTokenizer.from_pretrained(self._model_name)
            self._model = transformers.AutoModel.from_pretrained(self._model_name)
            self._model.eval()
            self._model.to(self._device)
            logger.info("ESM-2 loaded on %s", self._device)
            logger.info(
                "[AI:ESM-2:LOAD] END model=%s device=%s elapsed=%.2fs",
                self._model_name,
                self._device,
                time.monotonic() - _t0,
            )
            return True
        except Exception as exc:
            logger.error("Failed to load ESM-2: %s", exc)
            logger.info(
                "[AI:ESM-2:LOAD] END (failed) model=%s elapsed=%.2fs",
                self._model_name,
                time.monotonic() - _t0,
            )
            return False

    def _embed(self, aa_sequence: str):
        """Return mean-pooled embedding tensor for an amino-acid sequence."""
        torch = _torch
        inputs = self._tokenizer(
            aa_sequence,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=1022,
        )
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = self._model(**inputs)
        # Mean pool over sequence length (excluding special tokens via attention mask)
        mask = inputs["attention_mask"].unsqueeze(-1).float()
        mean_emb = (outputs.last_hidden_state * mask).sum(1) / mask.sum(1)
        return mean_emb

    def score(self, wildtype_aa: str, mutant_aa: str) -> float | None:
        """Return a variant impact score in [0, 1].

        Uses L2 distance between wild-type and mutant embeddings,
        normalised by a sigmoid to give [0, 1].

        Args:
            wildtype_aa: Wild-type amino-acid sequence (single-letter codes).
            mutant_aa:   Mutant amino-acid sequence.

        Returns:
            Float in [0, 1] or None if model is not available.
        """
        _t0 = time.monotonic()
        logger.info("[AI:ESM-2:SCORE] START")
        if not self._load():
            logger.info(
                "[AI:ESM-2:SCORE] END (unavailable) elapsed=%.2fs",
                time.monotonic() - _t0,
            )
            return None
        try:
            torch = _torch
            emb_wt = self._embed(wildtype_aa)
            emb_mut = self._embed(mutant_aa)
            l2_dist = torch.norm(emb_wt - emb_mut, dim=-1).item()
            # Sigmoid normalisation: distance of ~4 maps to 0.5
            score = round(1.0 / (1.0 + math.exp(-l2_dist + 4.0)), 4)
            logger.debug("ESM-2 score: %.4f (L2=%.4f)", score, l2_dist)
            logger.info("[AI:ESM-2:SCORE] END elapsed=%.2fs", time.monotonic() - _t0)
            return score
        except Exception as exc:
            logger.error("ESM-2 inference failed: %s", exc)
            logger.info(
                "[AI:ESM-2:SCORE] END (error) elapsed=%.2fs",
                time.monotonic() - _t0,
            )
            return None


# ─── Combined engine ──────────────────────────────────────────────────────────


class AiEngine:
    """Unified AI inference engine combining DNABERT-2 and ESM-2.

    Wraps both models behind a single interface and provides a
    ``combined_score`` that averages whichever streams are available.

    Args:
        cfg: Full pipeline config dict. Model settings are read from
             ``cfg["dnabert2"]`` and ``cfg["esm2"]``.
    """

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        db2_cfg = cfg.get("dnabert2", {}) or {}
        esm_cfg = cfg.get("esm2", {}) or {}

        self.dnabert2 = DnaBertEngine(
            model_name=db2_cfg.get("model_name", "zhihan1996/DNABERT-2-117M"),
            device=db2_cfg.get("device", "auto"),
            batch_size=int(db2_cfg.get("batch_size", 8)),
        )
        self.esm2 = Esm2Engine(
            model_name=esm_cfg.get("model_name", "facebook/esm2_t12_35M_UR50D"),
            device=esm_cfg.get("device", "auto"),
            batch_size=int(esm_cfg.get("batch_size", 4)),
        )

    def score_dna(self, ref_sequence: str, alt_sequence: str) -> float | None:
        """DNABERT-2 pathogenicity score for a DNA context pair."""
        return self.dnabert2.score(ref_sequence, alt_sequence)

    def score_protein(self, wildtype_aa: str, mutant_aa: str) -> float | None:
        """ESM-2 impact score for a wild-type / mutant protein pair."""
        return self.esm2.score(wildtype_aa, mutant_aa)

    def combined_score(
        self,
        dna_score: float | None,
        protein_score: float | None,
        dna_weight: float = 0.5,
        protein_weight: float = 0.5,
    ) -> float | None:
        """Weighted average of DNA and protein scores.

        Weights are re-normalised if only one stream is available.

        Returns:
            Float in [0, 1], or None if both scores are None.
        """
        available = {
            k: v for k, v in [("dna", dna_score), ("protein", protein_score)] if v is not None
        }
        if not available:
            return None
        weights = {"dna": dna_weight, "protein": protein_weight}
        total_w = sum(weights[k] for k in available)
        score = sum(weights[k] * available[k] for k in available) / total_w
        return round(score, 4)

    def is_available(self) -> bool:
        """Return True if at least one AI model can be loaded."""
        torch = _try_import_torch()
        transformers = _try_import_transformers()
        return bool(torch and transformers)
