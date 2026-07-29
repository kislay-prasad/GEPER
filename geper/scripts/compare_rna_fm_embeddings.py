"""
scripts/compare_rna_fm_embeddings.py
──────────────────────────────────────
Standalone empirical equivalence check between the old MultiMolecule
RNA-FM wrapper and the new official ml4bio/RNA-FM wrapper this project
migrated to (see MIGRATION_RNA_FM.md).

NOT run as part of the automated test suite -- it needs BOTH the old
(`multimolecule`) and new (`rna-fm`) packages installed simultaneously,
which this sandbox's disk/network couldn't accommodate (the default
PyPI `torch` package pulls in the full CUDA toolkit, several GB, and
the lean CPU-only wheels are hosted at download.pytorch.org rather than
pypi.org). Run this in an environment that already has your project's
normal dependencies installed, e.g.:

    pip install multimolecule rna-fm
    python scripts/compare_rna_fm_embeddings.py

Exit code is 0 if every sequence's cosine similarity clears
--min-cosine-similarity (default 0.999), 1 otherwise -- wire this into
CI once both packages can coexist in a build environment, so any
future weight/version drift between the two is caught automatically
rather than relying on a one-time manual check.
"""
from __future__ import annotations

import argparse
import sys

# Representative sequences: short, a longer one with more secondary
# structure potential, and one containing an ambiguous base (N) to
# exercise tokenizer edge-case handling identically on both sides.
TEST_SEQUENCES = {
    "short_18nt": "GGGUGCGAUCAUACCAGC",
    "medium_56nt_hairpin_like": "GGGUGCGAUCAUACCAGCACUAAUGCCCUCCUGGGAAGUCCUCGUGUUGCACCCCU",
    "contains_ambiguous_base": "GGGUGCGAUCANNACCAGCACUAAUGCCCUCCUGGGAAGUCC",
    "poly_a_tail_like": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
}


def embed_multimolecule(sequence: str):
    import torch
    from multimolecule import RnaFmModel, RnaTokenizer

    tokenizer = RnaTokenizer.from_pretrained("multimolecule/rnafm")
    model = RnaFmModel.from_pretrained("multimolecule/rnafm")
    model.eval()
    inputs = tokenizer(sequence, return_tensors="pt")
    with torch.no_grad():
        outputs = model(**inputs)
    hidden = outputs.last_hidden_state
    mask = inputs["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
    pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
    return pooled.squeeze(0)


def embed_official(sequence: str):
    import torch
    import fm

    model, alphabet = fm.pretrained.rna_fm_t12()
    model.eval()
    batch_converter = alphabet.get_batch_converter()
    _, _, tokens = batch_converter([("seq0", sequence)])
    with torch.no_grad():
        results = model(tokens, repr_layers=[model.num_layers])
    hidden = results["representations"][model.num_layers]
    pooled = hidden.mean(dim=1)  # single unpadded sequence: plain mean == masked mean
    return pooled.squeeze(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-cosine-similarity", type=float, default=0.999)
    args = parser.parse_args()

    import torch

    print(f"{'Sequence':<28} {'Len':>5} {'CosineSim':>10} {'Dim match':>10}")
    print("-" * 60)
    all_pass = True
    for name, seq in TEST_SEQUENCES.items():
        try:
            emb_a = embed_multimolecule(seq)
            emb_b = embed_official(seq)
        except Exception as exc:
            print(f"{name:<28} FAILED TO EMBED: {exc}")
            all_pass = False
            continue
        dim_match = emb_a.shape == emb_b.shape
        cos_sim = torch.nn.functional.cosine_similarity(
            emb_a.unsqueeze(0), emb_b.unsqueeze(0)
        ).item()
        ok = dim_match and cos_sim >= args.min_cosine_similarity
        all_pass = all_pass and ok
        print(f"{name:<28} {len(seq):>5} {cos_sim:>10.6f} {str(dim_match):>10}  {'OK' if ok else 'MISMATCH'}")

    print("-" * 60)
    if all_pass:
        print(f"PASS -- all sequences >= {args.min_cosine_similarity} cosine similarity, dims match.")
        sys.exit(0)
    else:
        print(
            "FAIL -- one or more sequences did not meet the equivalence bar. "
            "Do not consider the migration behavior-preserving until this passes; "
            "investigate whether config.py's RNA_FM variant matches MultiMolecule's "
            "checkpoint (both should be the base 12-layer model)."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
