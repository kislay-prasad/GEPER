"""
Evolutionary-conservation evidence sources: PhyloP, PhastCons, GERP++.

Same shape as `pipeline/gnomad/` and `pipeline/clingen/` (models.py /
utils.py / provider.py / cache.py / lookup.py), and the same two
independent, configurable query sources requirement those modules
already implement:

  - A local, per-base conservation track file the deployer has
    already provisioned (a bigWig file -- e.g. UCSC's own
    `hg38.phyloP100way.bw`), queried via the `bigWigSummary` CLI (part
    of UCSC's "kent" command-line tools) rather than a native-
    extension Python binding (`pyBigWig`), for the same
    dependency-avoidance reason `pipeline/vcf_parser.py`'s own module
    docstring gives for not using pysam/cyvcf2 -- GEPER never
    downloads or builds this file itself (multi-GB genome-wide
    tracks), exactly like gnomAD's own local sites-VCF.
  - UCSC's public Genome Browser REST API
    (https://api.genome.ucsc.edu/getData/track), used automatically
    when no local track file is configured (or the local query
    itself fails), unless offline mode is set. Verified directly
    against a real request before building this module (not assumed):
    `GET .../getData/track?genome=hg38;track=phyloP100way;chrom=chr17;
    start=7674857;end=7674860` returns real per-base phyloP values
    (confirmed against a known-conserved TP53 coding position: ~6.2-7.9,
    near this track's own reported max of 7.532 -- and a clearly lower,
    near-zero/negative value at an intergenic control locus).

One evidence source, three sub-scores added incrementally (PhyloP and
PhastCons integrated so far; GERP++ follows the same pattern) --
`ConservationAnnotation` carries whichever of
`phylop_score`/`phastcons_score`/`gerp_score` have been integrated so
far, `None` for the rest, so the ACMG PP3/BP4 extension
(`pipeline/acmg_rules.py`) and downstream report/JSON output only ever
have to widen, never restructure. `CompositeConservationProvider` runs
every active score type's own local-first/API-fallback chain and
merges them into one `ConservationAnnotation` per variant (see
`provider.py`'s own docstring).
"""
