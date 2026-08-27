"""
ESM-2 wrapper.

facebook/esm2_t33_650M_UR50D embeds the protein sequence translated
from the variant's coding-region context, capturing whether the
resulting amino-acid change lands in a functionally/structurally
important region of the protein.
"""

from typing import Any, Dict

from transformers import AutoTokenizer, EsmModel

from config import CONFIG
from models.base_model import BaseGenomicModel

# Pinned HuggingFace revision (commit SHA) for CONFIG.models.ESM2
# ("facebook/esm2_t33_650M_UR50D"), verified live via
# `git ls-remote https://huggingface.co/facebook/esm2_t33_650M_UR50D HEAD`,
# 2026-08-27. Without a revision pin, `from_pretrained` resolves
# whatever is currently the repo's default-branch tip -- a silent
# upstream change would move every future run's embeddings without any
# GEPER-side signal that anything changed.
_ESM2_REVISION = "08e4846e537177426273712802403f7ba8261b6c"


class ESM2Model(BaseGenomicModel):
    """Embeds protein sequences using ESM-2 for variant-effect context."""

    def cache_key(self) -> str:
        return "esm2"

    def _load_impl(self):
        model_id = CONFIG.models.ESM2
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, cache_dir=CONFIG.CACHE_DIR, revision=_ESM2_REVISION)
        self.model = EsmModel.from_pretrained(model_id, cache_dir=CONFIG.CACHE_DIR, revision=_ESM2_REVISION)
        self.model.to(self.device)
        self.model.eval()

    def reported_max_length(self):
        return self._resolve_safe_max_length(CONFIG.models.ESM2_MAX_SAFE_TOKENS)

    def _infer_impl(self, sequence: str, **kwargs) -> Dict[str, Any]:
        # Strip stop-codon markers ('*') that ProteinTranslator may emit;
        # ESM-2's tokenizer does not expect them mid-sequence.
        protein_sequence = sequence.replace("*", "").strip()

        # Defensive cap: translated ORFs are normally short, but a
        # pathologically long open reading frame (e.g. from a
        # structural-variant-scale sequence window) should never be
        # allowed to overflow ESM-2's trained context and risk a CUDA
        # error the way the untamed Nucleotide Transformer input did.
        max_length = kwargs.get("max_length") or self._resolve_safe_max_length(CONFIG.models.ESM2_MAX_SAFE_TOKENS)
        inputs = self.tokenizer(protein_sequence, return_tensors="pt", truncation=True, max_length=max_length)
        if inputs["input_ids"].shape[1] >= max_length:
            self.logger.warning(
                f"Protein sequence length {len(protein_sequence)} produced "
                f">= {max_length} tokens for ESM-2; truncated to the "
                "model's safe limit."
            )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        outputs = self.model(**inputs)
        hidden_states = outputs.last_hidden_state

        pooled = self.mean_pool(hidden_states, inputs["attention_mask"])
        cls_embedding = hidden_states[:, 0, :]

        return {
            "embedding_mean": pooled.squeeze(0).cpu().tolist(),
            "embedding_cls": cls_embedding.squeeze(0).cpu().tolist(),
            "embedding_dim": hidden_states.shape[-1],
            "num_residues": len(protein_sequence),
        }
