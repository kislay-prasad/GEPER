# GEPER BP7 Kaggle Verification — Step-by-Step

This proves BP7 (`pipeline/acmg_rules.py::ACMGRuleEngine._bp7`) triggers
correctly using **real** SpliceFormer + SpliceBERT model inference on a
**real** ClinVar variant (BRCA1 c.5175A>G, VCV000136552), on a GPU
environment with enough RAM to actually load both models.

## 1. Set up the Kaggle notebook

1. Go to kaggle.com → **Create** → **New Notebook**.
2. Right panel → **Settings**:
   - **Accelerator**: GPU T4 x2 (or P100 / any GPU option — this workload
     doesn't need much VRAM, it just needs more RAM than the local
     sandbox had).
   - **Internet**: **On** (required — the script downloads real model
     checkpoints from GitHub/Zenodo and fetches a real sequence window
     from Ensembl).

## 2. Paste the script

Open `bp7_kaggle_verification.py`. It's organized into 7 clearly
labeled sections:

```
===== CELL 1 =====   pip installs
===== CELL 2 =====   real variant + real Ensembl sequence fetch
===== CELL 3 =====   SpliceFormer vendored model code (MIT-licensed, verbatim)
===== CELL 4 =====   SpliceFormer: real checkpoint download + real inference
===== CELL 5 =====   SpliceBERT: real checkpoint download + real inference
===== CELL 6 =====   (optional) RNA-FM real-loading demo — not used by BP7
===== CELL 7 =====   BP7 rule logic + verdict + PASS/FAIL summary
```

Create 7 cells in your Kaggle notebook (or fewer — merging is fine,
Python doesn't care about cell boundaries) and paste each section in
order. The very first line of Cell 1 is commented out
(`# !pip install ...`) — uncomment it (remove the leading `# `) so it
actually runs, or just run:

```
!pip install -q einops "transformers>=4.30"
```

as the first line of your first cell.

## 3. Run all cells, top to bottom

Expected timing (rough, varies by Kaggle's current load):
- Cell 4 (SpliceFormer): checkpoint is one ~tens-of-MB PyTorch state
  dict from GitHub — should be under a minute.
- Cell 5 (SpliceBERT): downloads a ~208 MiB Zenodo archive, extracts
  one of its three bundled checkpoints — a few minutes.
- Cell 6 (RNA-FM, optional): can be slow or fail due to a known
  upstream download-endpoint reliability issue — this is fine, it
  doesn't affect BP7. If it hangs, interrupt just that cell and move on.
- Cell 7: fast (pure Python, no model calls).

## 4. What success looks like

Cell 7 ends with a `--- PASS/FAIL ---` block. **PASS** means BP7
triggered using real SpliceFormer + SpliceBERT output (the expected
direction for a real ClinVar "Likely benign" synonymous variant, though
it's a genuine model result — not scripted). If it instead prints the
**INFO** branch, that's not a script bug — it means one of the real
models predicted a splicing effect for this specific sequence window;
report the full output either way, it's useful either way.

## 5. Send the results back

Copy everything printed from the
`======================================================================`
banner right before `BP7 VERIFICATION` down through the final
`--- and send it back for review. ---` line, and paste it back into
the chat. That block includes:
- Real SpliceFormer classification + score
- Real SpliceBERT classification + score
- The BP7 verdict (triggered / not_triggered) + rationale
- The PASS/INFO/UNEXPECTED summary line

If you'd rather share the whole notebook, you can also just make it
public (or "Share" → unlisted link) and send the URL — either works.
