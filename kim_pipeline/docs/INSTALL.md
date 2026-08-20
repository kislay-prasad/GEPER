# GEPER v8 — Installation Guide

## System Requirements

- Python 3.10+
- 8 GB RAM minimum (16 GB recommended for AI models)
- GPU optional (CUDA 11.8+ for DNABERT2/ESM2 acceleration)
- Linux / macOS (Windows: WSL2 recommended)

## Quick Install

```bash
git clone https://github.com/your-org/geper.git
cd geper_v8
pip install -r requirements.txt
```

## Install by Feature Set

```bash
# Core only (API + ACMG + clinical databases, no AI models)
pip install -e .

# With AI models (DNABERT2 + ESM2)
pip install -e ".[ai]"

# With bioinformatics tools (pysam, HGVS, biopython)
pip install -e ".[bio]"

# With PDF report generation
pip install -e ".[pdf]"

# Everything
pip install -e ".[all]"
```

## Clinical Database Setup (Optional but Recommended)

### ClinVar (local — fastest)
```bash
mkdir -p /data/clinvar
wget https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz \
     -O /data/clinvar/variant_summary.txt.gz
```
Set in `config/production.yaml`:
```yaml
clinvar:
  tsv_gz_path: "/data/clinvar/variant_summary.txt.gz"
```
Without this file, GEPER falls back to live NCBI Entrez REST queries (requires internet, rate-limited to 3 req/s without API key).

### gnomAD (local — optional)
```bash
# Download per-chromosome files for GRCh38
for CHR in {1..22} X Y; do
  wget "https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/genomes/gnomad.genomes.v4.1.sites.chr${CHR}.vcf.bgz" \
       -O /data/gnomad/gnomad.genomes.v4.1.sites.chr${CHR}.vcf.bgz
  wget "https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/genomes/gnomad.genomes.v4.1.sites.chr${CHR}.vcf.bgz.tbi" \
       -O /data/gnomad/gnomad.genomes.v4.1.sites.chr${CHR}.vcf.bgz.tbi
done
```
Without this, GEPER uses the gnomAD GraphQL API (live, requires internet).

### OMIM (retired 2026-08-20)
**OMIM has been removed from kim_pipeline effective 2026-08-20 due to licensing restrictions.** OMIM's terms state: "This resource is intended for purely research purposes" and "Commercial use of the resource would require licensing." GEPER is being developed for eventual clinical/diagnostic deployment, which falls outside OMIM's stated research-use scope.

- Previous setup (now deprecated): free registration at https://omim.org/api, genemap2.txt download from https://data.omim.org/downloads/
- Code status: `pipeline/omim/` module and all caller references have been removed from the pipeline
- Future reinstatement: possible if a commercial license from OMIM is obtained
- For new deployments: do not attempt to configure OMIM; existing workflows continue without it

See `geper/DATA_SOURCE_LICENSE_AUDIT.md` (OMIM retirement entry) for the full regulatory analysis.

### RefSeq GFF3 (for transcript-aware RNA analysis)
```bash
mkdir -p /data/refseq
wget https://ftp.ncbi.nlm.nih.gov/refseq/H_sapiens/annotation/GRCh38_latest/refseq_identifiers/GRCh38_latest_genomic.gff.gz \
     -O /data/refseq/GRCh38_latest_genomic.gff.gz
```
Set in config:
```yaml
rna_analysis:
  refseq_gff: "/data/refseq/GRCh38_latest_genomic.gff.gz"
```

## Configuration

Copy and edit the default config:
```bash
cp config/default.yaml config/production.yaml
# Edit paths and API keys
vim config/production.yaml
```

## PDF Report Generation

GEPER tries WeasyPrint first, then wkhtmltopdf:

```bash
# Option 1: WeasyPrint (recommended)
pip install weasyprint
# On Ubuntu/Debian also install:
sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0

# Option 2: wkhtmltopdf
sudo apt-get install wkhtmltopdf
```

## Start the API Server

```bash
# Development
python main.py serve

# With custom config and port
python main.py serve --config config/production.yaml --port 8080

# Production (gunicorn + uvicorn workers)
pip install gunicorn
gunicorn geper.api.app:create_app -k uvicorn.workers.UvicornWorker \
  --workers 4 --bind 0.0.0.0:8000

# Docker
docker build -t geper-v8 .
docker run -p 8000:8000 \
  -v /data:/data \
  -e NCBI_API_KEY=your_key \
  geper-v8
```

## Run Tests

```bash
# All tests
pytest tests/test_geper_v8.py -v

# With coverage report (target >80%)
pytest tests/test_geper_v8.py -v --cov=geper --cov-report=term-missing

# Via main.py
python main.py test --coverage
```

## Analyze Your First Variant (CLI)

```bash
python main.py analyze \
  --chrom 17 \
  --pos 43057051 \
  --ref A \
  --alt T \
  --gene BRCA1 \
  --cadd 40.5 \
  --revel 0.92 \
  --output-dir ./reports
```

## Analyze a VCF File (CLI)

```bash
python main.py vcf \
  --input my_variants.vcf \
  --output-dir ./reports \
  --config config/production.yaml
```

## Environment Variables

| Variable         | Purpose                                      |
|------------------|----------------------------------------------|
| NCBI_API_KEY     | NCBI Entrez API key (10 req/s vs 3 req/s)    |
| GEPER_CONFIG     | Path to config YAML (overrides default)      |
| GEPER_OUTPUT_DIR | Default report output directory              |

## System Tools — Alignment & Variant Calling (pipeline/alignment, pipeline/variant_calling)

These stages shell out to real bioinformatics binaries — there is no
pure-Python fallback, and no result is fabricated when a tool is
missing (the stage raises a clear error naming the missing tool
instead).

```bash
# Debian/Ubuntu — bwa, minimap2, samtools, bcftools are all packaged:
apt install bwa minimap2 samtools bcftools

# FreeBayes is NOT packaged for apt/pip on most distros — install via
# bioconda or build from source:
conda install -c bioconda freebayes
# or: https://github.com/freebayes/freebayes#installation

# Optional, for multi-threaded FreeBayes (otherwise runs single-process):
conda install -c bioconda freebayes  # ships freebayes-parallel + fasta_generate_regions.py
```

Quick check of what's on PATH:

```bash
python -c "
from pipeline.alignment import bwa_runner, minimap2_runner
from pipeline.variant_calling import freebayes_runner
print('bwa/bwa-mem2:', bwa_runner.is_available())
print('minimap2:    ', minimap2_runner.is_available())
print('freebayes:   ', freebayes_runner.is_available())
"
```

Run the alignment + variant-calling stages directly:

```python
from pipeline.alignment.stage import AlignmentStage
from pipeline.variant_calling.stage import VariantCallingStage

align_result = AlignmentStage().run(
    fastq_r1="sample_R1.fastq.gz",
    fastq_r2="sample_R2.fastq.gz",
    reference_fasta="GRCh38.fasta",
    output_dir="./work/sample01",
    sample_id="sample01",
)
# -> ./work/sample01/aligned.sorted.bam (+ .bai), alignment_metrics.json

vc_result = VariantCallingStage().run(
    bam_path=align_result.sorted_bam_path,
    reference_fasta="GRCh38.fasta",
    output_dir="./work/sample01",
    sample_id="sample01",
)
# -> ./work/sample01/variants.vcf, filtered_variants.vcf
```
