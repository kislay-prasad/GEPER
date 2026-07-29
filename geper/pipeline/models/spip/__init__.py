"""
SPiP plugin support package.

Unlike every other plugin in `pipeline/models/` (Enformer, Borzoi,
SpliceFormer, SpliceBERT -- all PyTorch, all in-process), SPiP
(github.com/LBGC-CFB/SPiP, MIT license) is an R script that runs a
randomForest classifier cascading several splicing-prediction
components (SPiCE, MaxEntScan, Branch Point Predictor, ESR/ESE/ESS
hexamer scoring) reimplemented natively in R -- confirmed by reading
`SPiPv2.1_main.r`/`RefFiles/SPiP_libs/*.r` directly: no `system()`/
`shell()` calls anywhere, so there are no *separate* external tool
binaries to install beyond R itself and the three CRAN packages it
uses (`foreach`, `doParallel`, `randomForest`).

Following the same subprocess-isolation rationale already established
in this very codebase for a foreign-language integration
(`bridge/combined_pipeline.py`'s own module docstring, for the
Kim-pipeline bridge): rather than trying to port SPiP's ~65KB of R
scoring logic into Python (which would mean re-deriving and
re-validating its randomForest model, SPiCE thresholds, and ESR
hexamer tables independently -- a correctness risk with no upside),
`pipeline/models/spip_plugin.py` vendors the official, unmodified R
source (`vendor/`, this package) and invokes it as a subprocess via
`Rscript`, exactly the way a user would run it standalone
(`Rscript SPiPv2.1_main.r -I in.vcf -O out.txt`), then parses its
tab-delimited output back into GEPER's usual plugin result shape.

`vendor/` holds ONLY source code/small lookup tables (the R scripts
themselves, `VPP_table.txt`/`VPN_table.txt`, header templates) --
verbatim from the upstream repository, MIT licensed. It does NOT hold
SPiP's own "weights": the trained randomForest model (`model.RData`),
RefSeq transcript annotation database (`dataRefSeq<genome>.RData`,
`RefFiles.RData`), and genomic sequence database
(`transcriptome_<genome>.RData`, ~370-400MB, hosted separately on
SourceForge -- not even in the GitHub repo) are all downloaded into
`pipeline.models.cache.WeightCache`'s on-disk cache at first use, the
same "vendor code, download data" split `pipeline/models/
spliceformer/` already uses. See `loader.py` for the exact URLs and
the primary-source sourcing note for each.
"""
