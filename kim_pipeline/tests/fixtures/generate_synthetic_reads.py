"""
tests/fixtures/generate_synthetic_reads.py
─────────────────────────────────────────────
Synthetic reference + paired-end FASTQ generator for testing
pipeline/alignment and pipeline/variant_calling against real tools,
with no real patient data and no external read simulator.

Approach: generate a random ~4kb reference, inject a handful of known
SNPs and a small indel at known positions to produce an "individual"
genome, then slice fixed-length, overlapping paired-end reads off of
it (forward strand for R1, reverse-complement for R2), with realistic
but high-quality Phred+33 scores. This is synthetic *input data* for
testing — not a mock of the pipeline itself; the actual aligners and
caller run against it for real.
"""
from __future__ import annotations
import random
from pathlib import Path

COMPLEMENT = str.maketrans("ACGT", "TGCA")


def revcomp(seq: str) -> str:
    return seq.translate(COMPLEMENT)[::-1]


def generate_reference(length: int = 4000, seed: int = 42) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice("ACGT") for _ in range(length))


def inject_variants(reference: str, seed: int = 7):
    """Return (mutated_sequence, list of (0-based pos, ref, alt, kind))."""
    rng = random.Random(seed)
    seq = list(reference)
    variants = []

    # 4 well-spaced SNPs
    snp_positions = [500, 1200, 2100, 3000]
    for pos in snp_positions:
        ref_base = seq[pos]
        alt_base = rng.choice([b for b in "ACGT" if b != ref_base])
        seq[pos] = alt_base
        variants.append((pos, ref_base, alt_base, "SNP"))

    # 1 small deletion (remove 2 bases at pos 1800)
    del_pos = 1800
    ref_bases = "".join(seq[del_pos:del_pos + 3])  # incl. anchor base
    seq[del_pos + 1:del_pos + 3] = []
    variants.append((del_pos, ref_bases, ref_bases[0], "DEL"))

    return "".join(seq), variants


def write_fasta(path: str, name: str, sequence: str):
    with open(path, "w") as f:
        f.write(f">{name}\n")
        for i in range(0, len(sequence), 70):
            f.write(sequence[i:i + 70] + "\n")


def simulate_paired_reads(
    sequence: str,
    read_length: int = 100,
    insert_size: int = 300,
    step: int = 20,
    seed: int = 99,
):
    """Slide a window across `sequence`, emitting (r1_seq, r2_seq) pairs
    at `step`-base intervals, each `read_length` long, `insert_size` apart."""
    rng = random.Random(seed)
    pairs = []
    n = len(sequence)
    pos = 0
    while pos + insert_size <= n:
        r1 = sequence[pos: pos + read_length]
        frag_end = pos + insert_size
        r2_fwd = sequence[frag_end - read_length: frag_end]
        r2 = revcomp(r2_fwd)
        pairs.append((r1, r2))
        pos += step
    return pairs


def quality_string(length: int, base_q: int = 35) -> str:
    return chr(base_q + 33) * length


def write_fastq(path: str, reads, sample_prefix: str):
    with open(path, "w") as f:
        for i, seq in enumerate(reads):
            f.write(f"@{sample_prefix}.{i}\n{seq}\n+\n{quality_string(len(seq))}\n")


def build_fixture_set(out_dir: str):
    """Write reference.fasta, sample_R1.fastq, sample_R2.fastq, and
    return the list of injected variants for assertion in tests."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    ref_seq = generate_reference()
    write_fasta(str(out / "reference.fasta"), "synthetic_chr1", ref_seq)

    sample_seq, variants = inject_variants(ref_seq)
    pairs = simulate_paired_reads(sample_seq)

    write_fastq(str(out / "sample_R1.fastq"), [p[0] for p in pairs], "read")
    write_fastq(str(out / "sample_R2.fastq"), [p[1] for p in pairs], "read")

    return {
        "reference_fasta": str(out / "reference.fasta"),
        "fastq_r1": str(out / "sample_R1.fastq"),
        "fastq_r2": str(out / "sample_R2.fastq"),
        "variants": variants,
        "n_read_pairs": len(pairs),
    }


if __name__ == "__main__":
    info = build_fixture_set("tests/fixtures/synthetic_run")
    print(f"Wrote {info['n_read_pairs']} read pairs")
    print(f"Injected variants: {info['variants']}")
