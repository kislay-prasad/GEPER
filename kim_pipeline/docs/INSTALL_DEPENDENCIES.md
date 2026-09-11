# Bij AI sequencing-analysis component — External Bioinformatics Tool Installation Guide

The sequencing-analysis component's core pipeline (FASTQ → Alignment → Variant Calling → Annotation →
Report) depends on a small set of external, non-Python bioinformatics
binaries. This document explains how to install them on every supported
platform.

For Python package installation (PyYAML, FastAPI, etc.) see
[`docs/INSTALL.md`](INSTALL.md). This document covers **system binaries
only**.

## Required vs. optional tools

| Tool        | Required? | Used for                                   |
|-------------|-----------|---------------------------------------------|
| `bwa`       | **Yes**   | Short-read alignment (default aligner)      |
| `samtools`  | **Yes**   | BAM sorting, indexing, alignment metrics    |
| `freebayes` | **Yes**   | Variant calling                             |
| `bcftools`  | **Yes**   | VCF normalization and filtering             |
| `minimap2`  | Optional  | Alternate aligner (only if configured as the active aligner) |
| `vep`       | Optional  | External Ensembl VEP annotation (the sequencing-analysis component degrades gracefully without it) |

Before running the pipeline, the sequencing-analysis component performs a single consolidated
dependency check at startup (see `python main.py verify-environment` and
the automatic pre-flight check in `python main.py analyze`). If a
**required** tool is missing, the sequencing-analysis component fails immediately with one clear
error listing everything that's missing — it will not fail partway
through a multi-hour run.

---

## Ubuntu / Debian (`apt`)

```bash
sudo apt-get update
sudo apt-get install -y \
    bwa \
    samtools \
    bcftools \
    minimap2 \
    build-essential cmake zlib1g-dev libbz2-dev liblzma-dev \
    libcurl4-openssl-dev libssl-dev libncurses5-dev pkg-config \
    python3-pip
```

`freebayes` is not reliably packaged as a recent version on all Ubuntu
releases; the safest install is via conda/bioconda (see below) or by
building from source:

```bash
git clone --recursive https://github.com/freebayes/freebayes.git
cd freebayes
make -j"$(nproc)"
sudo cp bin/freebayes /usr/local/bin/
```

### Verify

```bash
bwa 2>&1 | head -3
samtools --version | head -1
bcftools --version | head -1
freebayes --version
minimap2 --version
```

---

## Conda / Bioconda (recommended — simplest, works on Linux and macOS)

```bash
conda create -n geper -c bioconda -c conda-forge \
    bwa samtools bcftools freebayes minimap2 python=3.11
conda activate geper
pip install -r requirements.txt
```

This is the recommended route for reproducible research environments,
since bioconda pins tested, compatible versions of all five tools
together.

---

## Docker

A minimal Dockerfile that installs all required tools via bioconda:

```dockerfile
FROM continuumio/miniconda3:latest

RUN conda install -y -c bioconda -c conda-forge \
        bwa samtools bcftools freebayes minimap2 python=3.11 \
    && conda clean -afy

WORKDIR /app
COPY . /app
RUN pip install -r requirements.txt

ENTRYPOINT ["python", "main.py"]
```

Build and run:

```bash
docker build -t geper:latest .
docker run --rm -v "$(pwd)/data:/data" geper:latest verify-environment
docker run --rm -v "$(pwd)/data:/data" geper:latest analyze \
    --r1 /data/sample_R1.fastq.gz --r2 /data/sample_R2.fastq.gz \
    --ref /data/GRCh38.fasta --output-dir /data/out
```

Using Docker isolates the sequencing-analysis component from host-level version drift and is the
most reliable way to guarantee `verify-environment` reports PASS on any
machine with Docker installed.

---

## macOS (Homebrew)

```bash
brew update
brew install bwa samtools bcftools minimap2 freebayes
```

If `brew install freebayes` is unavailable on your macOS/Homebrew
version, fall back to conda/bioconda (above), which is the most
consistently maintained path for freebayes on macOS.

### Verify

```bash
which bwa samtools bcftools freebayes minimap2
```

---

## Windows (via WSL2 — recommended)

Native Windows binaries for these tools are not maintained; run the sequencing-analysis component
inside WSL2 (Ubuntu) instead:

1. Install WSL2 and an Ubuntu distribution:
   ```powershell
   wsl --install -d Ubuntu
   ```
2. Open the Ubuntu shell and follow the **Ubuntu / Debian** or
   **Conda / Bioconda** instructions above.
3. Run the sequencing-analysis component entirely from within the WSL2 shell — do not attempt to mix
   native Windows Python with WSL2-installed binaries.

---

## VEP (optional)

VEP is optional; the sequencing-analysis component runs without it (gene-dependent ACMG criteria such
as PM1/PM5/PP2/BP1 will be marked "not_evaluated" rather than failing).
To install it:

```bash
# Bioconda (simplest)
conda install -c bioconda ensembl-vep

# Or via the official installer
git clone https://github.com/Ensembl/ensembl-vep.git
cd ensembl-vep
perl INSTALL.pl
```

Installing the `vep` binary and a cache (`vep.cache_dir`) is enough to get
consequence terms and HGVS notation. It is **not** enough to get
CADD/REVEL/AlphaMissense scores (used for PP3/BP4 computational evidence)
-- those are separate VEP plugins, and each needs its own data file
downloaded independently. Leaving them unconfigured is fully supported:
The sequencing-analysis component runs VEP without them and those scores simply come back absent
(other PP3/BP4 evidence, and non-computational ACMG criteria, are
unaffected). To enable them:

1. Get the plugin `.pm` files (bundled with the VEP installer above, or
   `perl INSTALL.pl --PLUGINS all`) into a directory, e.g. `~/.vep/Plugins`.
2. Download each plugin's data file separately:
   - **CADD**: https://cadd.gs.washington.edu/download (`whole_genome_SNVs.tsv.gz` + its `.tbi`, GRCh38)
   - **REVEL**: https://sites.google.com/site/revelgenomics/downloads (pre-formatted VEP plugin release)
   - **AlphaMissense**: https://console.cloud.google.com/storage/browser/dm_alphamissense (`AlphaMissense_hg38.tsv.gz`)
3. Point the sequencing-analysis component at both in config:
   ```yaml
   vep:
     dir_plugins: "/home/you/.vep/Plugins"
     cadd_data: "/data/cadd/whole_genome_SNVs.tsv.gz"
     revel_data: "/data/revel/revel.tsv.gz"
     alphamissense_data: "/data/alphamissense/AlphaMissense_hg38.tsv.gz"
   ```

A bare plugin name with no data file is not a valid VEP invocation --
The sequencing-analysis component only ever requests a plugin when its `*_data` config key is set, so
partial setup (e.g. CADD configured, REVEL and AlphaMissense not) is safe
and simply yields fewer computational scores, not a failure.

---

## Automated installer

For Ubuntu/Debian-based systems, `install_dependencies.sh` (repo root)
automates the `apt` + source-build path above for all five required
tools without touching any pipeline code:

```bash
chmod +x install_dependencies.sh
./install_dependencies.sh
```

---

## After installing: verify everything at once

```bash
python main.py verify-environment
```

This checks Python version, RAM, CPU, CUDA/GPU, disk space, and every
tool listed above, and prints a PASS / WARNING / FAIL summary — run this
once after any environment change, before kicking off a real analysis.
