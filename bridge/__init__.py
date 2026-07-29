"""
bridge
──────
Connects the Kim pipeline (FASTQ -> filtered_variants.vcf) to the current
GEPER pipeline (VCF -> Clinical Report) by invoking each project's own,
unmodified entry point in its own subprocess.

This package does NOT reimplement, copy, or duplicate any logic from
either project. It only:
  1. Runs Kim's `main.py analyze --mode vcf_only` to produce
     filtered_variants.vcf.
  2. Locates that VCF from Kim's own checkpoint.json.
  3. Runs GEPER's `main.py --vcf <that file>` to produce the clinical
     report.

Kim and GEPER are kept in separate Python processes deliberately: both
projects define top-level packages/modules with the same names
(`pipeline`, etc.), so importing both in a single interpreter would
require renaming or merging modules -- exactly what this integration is
explicitly required NOT to do. Subprocess isolation keeps each project's
code, dependencies, and import namespace completely untouched.
"""
