#!/usr/bin/env python3
"""
generate_test_dataset.py
──────────────────────────
Generates a small, fast, biologically real FASTQ/reference validation
dataset for the Kim -> GEPER combined pipeline.

Design (see README_TEST_DATASET.md for full rationale):

  * Reference: the real revised Cambridge Reference Sequence (rCRS,
    NCBI accession NC_012920.1) — the actual human mitochondrial genome
    used as contig "MT" in GRCh38. 16,569 bp, single contig, GC ~44%
    (comfortably inside Kim's QC GC-fraction thresholds), tiny enough
    to align in well under a second in Google Colab.
  * Variant: reads are generated from a copy of the reference carrying
    a single homozygous substitution at 1-based position 3243 (A>G) —
    the real, well-known pathogenic m.3243A>G variant in MT-TL1
    (MELAS/MIDD), so the workflow exercises a clinically meaningful
    variant rather than an arbitrary one.
  * Reads: 150 bp single-end, tiled across the whole genome at a 9 bp
    step (≈16.7x average coverage — comfortably above Kim's
    min_total_reads=100 and variant_calling filter_min_depth=10
    thresholds), with a low, realistic per-base sequencing error rate
    and Phred quality scores well above the QC min_mean_quality=20
    threshold.
"""

from __future__ import annotations

import random
from pathlib import Path

random.seed(1729)  # deterministic dataset

HERE = Path(__file__).resolve().parent
RAW_REF_PATH = HERE / "rcrs_raw.fasta"
OUT_DIR = HERE / "dataset"
OUT_DIR.mkdir(exist_ok=True)

VARIANT_POS_1BASED = 3243     # m.3243A>G (MT-TL1, MELAS/MIDD) — real ClinVar pathogenic variant
VARIANT_REF = "A"
VARIANT_ALT = "G"

READ_LENGTH = 150
STEP = 9                       # -> ~16.7x average coverage
BASE_ERROR_RATE = 0.001        # 0.1% per-base sequencing error, realistic for Illumina
QUAL_HIGH = "I"                # Phred 40 (Illumina 1.8+ encoding)
QUAL_LOW = "5"                 # Phred 20, used only at simulated error positions

BASES = "ACGT"


def load_reference() -> str:
    lines = RAW_REF_PATH.read_text().splitlines()
    seq = "".join(l.strip() for l in lines if not l.startswith(">"))
    return seq.upper()


def apply_variant(seq: str) -> str:
    seq_list = list(seq)
    idx = VARIANT_POS_1BASED - 1
    assert seq_list[idx] == VARIANT_REF, (
        f"Reference base mismatch at {VARIANT_POS_1BASED}: "
        f"expected {VARIANT_REF}, found {seq_list[idx]}"
    )
    seq_list[idx] = VARIANT_ALT
    return "".join(seq_list)


def resolve_reference_n(seq: str) -> str:
    """The real rCRS has exactly one 'N' (position 3107, a historical
    numbering-preservation artifact, not a real ambiguous base in vivo).
    Both the delivered reference.fasta and the read-simulation source
    resolve it ONCE, deterministically, to the same fixed real base --
    never per-read-random -- so it can't produce a spurious split-allele
    call in the output VCF, and a literal FASTA 'N' never has to survive
    into a REF=N VCF record (non-standard, and liable to break strict
    downstream REF/ALT handling)."""
    idx = seq.find("N")
    if idx == -1:
        return seq
    fixed = random.choice(BASES)
    return seq[:idx] + fixed + seq[idx + 1:]


def simulate_read(seq: str, start: int, length: int) -> tuple[str, str]:
    """Return (read_sequence, quality_string) for a window starting at
    0-based `start`, injecting rare sequencing errors. `seq` must already
    have any reference 'N' resolved to a fixed real base (see
    `resolve_reference_n`) so read generation is fully deterministic."""
    window = seq[start:start + length]
    read_bases = []
    quals = []
    for b in window:
        if random.random() < BASE_ERROR_RATE:
            choices = [x for x in BASES if x != b]
            b = random.choice(choices)
            quals.append(QUAL_LOW)
        else:
            quals.append(QUAL_HIGH)
        read_bases.append(b)
    return "".join(read_bases), "".join(quals)


def revcomp(seq: str) -> str:
    comp = str.maketrans("ACGT", "TGCA")
    return seq.translate(comp)[::-1]


def main() -> None:
    ref_seq = load_reference()
    assert len(ref_seq) == 16569, f"Unexpected rCRS length: {len(ref_seq)}"

    # Resolve the single rCRS numbering-artifact N ONCE (see note below),
    # then derive both the delivered reference and the variant-carrying
    # read-source sequence from that same resolved base, so they never
    # disagree with each other.
    resolved_ref_seq = resolve_reference_n(ref_seq)
    mutant_seq = apply_variant(resolved_ref_seq)
    reference_seq_for_output = resolved_ref_seq
    read_source_seq = mutant_seq

    # ── Write reference.fasta (rCRS, with the single position-3107 numbering
    #    artifact resolved to a fixed real base — see resolve_reference_n) ──
    ref_out = OUT_DIR / "reference.fasta"
    with open(ref_out, "w") as f:
        f.write(
            ">MT rCRS (NC_012920.1)-derived, human mitochondrial genome, "
            "GRCh38 chrM equivalent; position 3107 (historical rCRS "
            "numbering-artifact N) resolved to a fixed base for VCF "
            "compatibility — see README_TEST_DATASET.md\n"
        )
        for i in range(0, len(reference_seq_for_output), 70):
            f.write(reference_seq_for_output[i:i + 70] + "\n")

    # ── Generate reads from the mutant sequence, tiled across the genome ──
    n = len(read_source_seq)
    starts = list(range(0, n - READ_LENGTH + 1, STEP))

    reads_out = OUT_DIR / "reads_1.fastq"
    read_count = 0
    depth_at_variant = 0
    with open(reads_out, "w") as f:
        for i, start in enumerate(starts):
            forward = (i % 2 == 0)  # alternate strand for realism
            read_seq, qual = simulate_read(read_source_seq, start, READ_LENGTH)
            if not forward:
                read_seq = revcomp(read_seq)
                qual = qual[::-1]
            read_id = f"@SIM.MT.{i:05d}/1"
            f.write(f"{read_id}\n{read_seq}\n+\n{qual}\n")
            read_count += 1
            if start <= (VARIANT_POS_1BASED - 1) < start + READ_LENGTH:
                depth_at_variant += 1

    print(f"reference.fasta : {ref_out}  ({len(reference_seq_for_output)} bp, GC={100*sum(c in 'GC' for c in reference_seq_for_output)/len(reference_seq_for_output):.1f}%)")
    print(f"reads_1.fastq   : {reads_out}  ({read_count} reads, {READ_LENGTH} bp each)")
    print(f"Approx. coverage at variant locus (pos {VARIANT_POS_1BASED}): {depth_at_variant}x")
    print(f"Variant introduced: MT:{VARIANT_POS_1BASED} {VARIANT_REF}>{VARIANT_ALT} (m.3243A>G, MT-TL1, MELAS/MIDD)")


if __name__ == "__main__":
    main()
