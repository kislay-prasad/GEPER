# GEPER `requirements.txt` — First Working Install Recipe (2026-08-20)

This records the first verified, from-scratch build of `geper/`'s
declared dependency set that actually resolved cleanly and let the
pipeline import. Built by Andy in an isolated venv, outside the git
tree, with the shared system `site-packages` left untouched throughout.
See `DATA_PROVENANCE.md`'s "`requirements.txt`'s build status" entry
for what this does and does not prove (build success, not a completed
end-to-end run).

## 1. Python version: 3.12.10, not the box's default

Use Python 3.11 or 3.12 specifically — `requirements.txt`'s own header
comment states this is forced by `evo2`'s own PyPI metadata
(`requires-python: >=3.11,<3.13`), not a GEPER preference. This box's
default Python is 3.14, which is both outside that range and has
markedly weaker prebuilt-wheel coverage today for `torch`/`tensorflow`
than 3.11/3.12 do — a fresh install on 3.14 would fight the wheel
resolver for reasons unrelated to `requirements.txt` itself.

```bash
py -3.12 -m venv <path-outside-the-git-tree>
```

Then upgrade pip before installing anything else (25.0.1 → 26.2.1 in
this build; exact target version isn't load-bearing, just don't skip
the upgrade on an old bundled pip).

## 2. Install order matters — do not run one flat `pip install -r requirements.txt` pass

Installing everything in a single pass is what previous attempts on
this box did, and is not what this recipe does. Install in five
discrete steps, in this order:

1. **`torch==2.7.1` alone, first.**
   ```bash
   pip install torch==2.7.1
   ```
   On a CPU-only install intent (as here), this resolves to the CPU
   wheel from **plain PyPI — no custom `--index-url` needed**. Verified
   `torch.__version__ == "2.7.1+cpu"`, `torch.cuda.is_available() ==
   False`. (This build's box does have a real CUDA GPU, an RTX 4070
   Laptop — CPU was chosen deliberately for this verification pass, not
   because CUDA wheels are unavailable.)

2. **One batch install of the core dependency set**, pinned/ranged
   exactly as `requirements.txt` declares: `transformers>=5.12.1,<6.0.0`,
   `accelerate`, `einops==0.8.1`, `torchvision==0.22.1`, `omegaconf==2.3.1`,
   `torchaudio==2.7.1`, `biopython`, `requests`, `numpy`, `pandas`,
   `tqdm`, `safetensors`, `pyyaml`, `pydantic`, `reportlab`, `Pillow`.
   All resolved cleanly, zero conflicts. `transformers` resolved to
   `5.15.1`; `torchvision`/`torchaudio` matched the `torch==2.7.1` pin
   exactly (this is the pairing `geper/README.md` Section 5's
   setup-gotcha note warns about getting wrong).

3. **`tensorflow>=2.16.0,<3.0.0` alone.** Resolved to `2.21.0`. Kept
   separate from step 2 rather than batched in — matches
   `requirements.txt`'s own comment-block sequencing above the
   `tensorflow` line.

4. **`mmsplice==2.4.0 --no-deps`**, exactly as `requirements.txt`
   instructs — the `--no-deps` flag is required, not optional. This
   step prints pip resolver warnings about missing `cyvcf2`,
   `kipoiseq`, `pyfaidx`, `pyranges`. **These warnings are expected and
   harmless, not errors**: GEPER never imports `mmsplice`'s own Python
   package code, only its bundled `.h5` weight files (see
   `geper/README.md` Section 12, "Why this isn't `pip install
   mmsplice`"), so the packages `mmsplice`'s unused `__init__.py` would
   otherwise need are irrelevant here.

5. **`rna-fm>=0.2.2`**. Resolved to `0.2.2`.

`evo2` is **deliberately excluded** from this recipe. It requires a
manual, CUDA-toolkit-matched build (`requirements.txt`'s own comment
block above the `evo2` line documents the exact Arc Institute install
sequence) that cannot be safely or quickly done via a bare install pass
in a fresh or CPU-only environment — its absence here is by design,
not a gap in this recipe.

## 3. Result

All 82 resolved packages installed with **zero dependency conflicts**.
`from pipeline.orchestrator import GeperPipeline` imported cleanly —
confirmed live, no errors. Disk footprint: 3.53 GB.

## 4. Ground truth: `pip freeze` (82 packages, captured after step 5 above)

```
absl-py==2.5.0
accelerate==1.14.0
annotated-doc==0.0.5
annotated-types==0.8.0
antlr4-python3-runtime==4.9.3
anyio==4.14.2
astunparse==1.6.3
biopython==1.88
certifi==2026.7.22
charset-normalizer==3.5.1
click==8.4.2
colorama==0.4.6
einops==0.8.1
filelock==3.32.3
flatbuffers==25.12.19
fsspec==2026.7.0
gast==0.7.0
google-pasta==0.2.0
grpcio==1.83.0
h11==0.16.0
h5py==3.14.0
hf-xet==1.6.0
httpcore==1.0.9
httpx==0.28.1
huggingface_hub==1.28.0
idna==3.19
Jinja2==3.1.6
joblib==1.5.3
keras==3.15.1
libclang==18.1.1
markdown-it-py==4.2.0
MarkupSafe==3.0.3
mdurl==0.1.2
ml_dtypes==0.6.0
mmsplice==2.4.0
mpmath==1.3.0
namex==0.1.0
narwhals==2.24.0
networkx==3.6.1
numpy==2.5.2
omegaconf==2.3.1
opt_einsum==3.4.0
optree==0.20.0
packaging==26.3
pandas==3.0.5
pillow==12.3.0
protobuf==7.35.1
psutil==7.2.2
ptflops==0.7.5
pydantic==2.13.4
pydantic_core==2.46.4
Pygments==2.21.0
python-dateutil==2.9.0.post0
PyYAML==6.0.3
regex==2026.7.19
reportlab==5.0.0
requests==2.34.2
rich==15.0.0
rna-fm==0.2.2
safetensors==0.8.0
scikit-learn==1.9.0
scipy==1.18.0
setuptools==84.0.0
shellingham==1.5.4
six==1.17.0
sympy==1.14.0
tensorflow==2.21.0
termcolor==3.3.0
threadpoolctl==3.6.0
tokenizers==0.22.2
torch==2.7.1
torchaudio==2.7.1
torchvision==0.22.1
tqdm==4.70.0
transformers==5.15.1
typer==0.27.1
typing-inspection==0.4.4
typing_extensions==4.16.0
tzdata==2026.3
urllib3==2.7.0
wheel==0.48.0
wrapt==2.3.0
```

## 5. What this recipe does not cover

This recipe proves the declared dependency set (minus `evo2`) installs
cleanly and the pipeline imports. It does not prove a completed
end-to-end variant-processing run — see `DATA_PROVENANCE.md` for that
distinction and for what has and hasn't been demonstrated on this box.
A separate, unresolved finding — GEPER's own optional-plugin
auto-install (Enformer/Borzoi) can downgrade `transformers` underneath
an already-running process on a fresh install with default settings —
was surfaced during verification runs using this venv and is under
separate triage; it is not a defect in this build recipe itself.
