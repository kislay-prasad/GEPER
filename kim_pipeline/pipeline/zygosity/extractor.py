"""
pipeline/zygosity/extractor.py
───────────────────────────────
VCF genotype zygosity extraction — stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ZygosityResult:
    """Parsed zygosity and FORMAT fields for a single sample genotype."""

    zygosity: str          # one of the seven canonical values
    gt: str                # raw GT string (phasing preserved)
    ad: Optional[List[int]]
    dp: Optional[int]
    gq: Optional[int]
    ab: Optional[float]    # allele balance = AD[1] / sum(AD)
    phase_set: Optional[str]

    def is_het(self) -> bool:
        return self.zygosity == "heterozygous"

    def is_hom_alt(self) -> bool:
        return self.zygosity == "homozygous_alt"

    def is_no_call(self) -> bool:
        return self.zygosity == "no_call"


class ZygosityExtractor:
    """Parse VCF FORMAT fields into a ZygosityResult."""

    @staticmethod
    def extract(
        gt_string: str,
        format_keys: List[str],
        format_values: List[str],
        alt_index: int = 1,
    ) -> ZygosityResult:
        """Extract zygosity and FORMAT values for one sample.

        Args:
            gt_string:     Raw GT value (e.g. "0/1", "1|1", "0/1/2", "./.").
            format_keys:   List of FORMAT keys (e.g. ["GT","AD","DP","GQ","PS"]).
            format_values: List of FORMAT values for this sample, same order.
            alt_index:     FIX 5 — 1-based index of the ALT allele being evaluated.
                           For biallelic VCFs this is always 1 (AD[0]=REF, AD[1]=ALT).
                           For multiallelic records split into separate AnnotatedVariants,
                           pass the 1-based allele index so AB = AD[alt_index] / sum(AD).

        Returns:
            ZygosityResult with all parsed fields.
        """
        fmt = dict(zip(format_keys, format_values))

        # ── GT → alleles (strip phasing separator) ──────────────────────────
        # Normalise phased (|) and unphased (/) into a uniform list of alleles
        alleles = re.split(r"[/|]", gt_string)

        zygosity = ZygosityExtractor._determine_zygosity(alleles, gt_string)

        # ── AD ───────────────────────────────────────────────────────────────
        ad: Optional[List[int]] = None
        raw_ad = fmt.get("AD", ".")
        if raw_ad and raw_ad != ".":
            try:
                ad = [int(x) for x in raw_ad.split(",")]
            except ValueError:
                ad = None

        # ── DP ───────────────────────────────────────────────────────────────
        dp: Optional[int] = None
        raw_dp = fmt.get("DP", ".")
        if raw_dp and raw_dp != ".":
            try:
                dp = int(raw_dp)
            except ValueError:
                dp = None

        # ── GQ ───────────────────────────────────────────────────────────────
        gq: Optional[int] = None
        raw_gq = fmt.get("GQ", ".")
        if raw_gq and raw_gq != ".":
            try:
                gq = int(raw_gq)
            except ValueError:
                gq = None

        # ── AB ───────────────────────────────────────────────────────────────
        ab: Optional[float] = None
        if ad and len(ad) >= 2:
            total = sum(ad)
            if total > 0:
                # FIX 5: use alt_index (1-based) so multiallelic AB is correct.
                # AD = [REF_depth, ALT1_depth, ALT2_depth, ...]
                # For alt_index=1 → AD[1]; for alt_index=2 → AD[2], etc.
                _alt_ad_idx = max(1, min(alt_index, len(ad) - 1))
                ab = round(ad[_alt_ad_idx] / total, 6)

        # ── PS (phase set) ────────────────────────────────────────────────────
        phase_set: Optional[str] = None
        raw_ps = fmt.get("PS", ".")
        if raw_ps and raw_ps != ".":
            phase_set = raw_ps

        return ZygosityResult(
            zygosity=zygosity,
            gt=gt_string,
            ad=ad,
            dp=dp,
            gq=gq,
            ab=ab,
            phase_set=phase_set,
        )

    @staticmethod
    def _determine_zygosity(alleles: List[str], gt_string: str) -> str:
        # All no-call
        if all(a == "." for a in alleles):
            return "no_call"

        # Filter out dots for downstream logic
        called = [a for a in alleles if a != "."]

        # Any remaining dot → treat whole call as no_call if nothing called
        if not called:
            return "no_call"

        ploidy = len(alleles)

        # All ref
        if all(a == "0" for a in called):
            return "homozygous_ref"

        non_ref = [a for a in called if a != "0"]

        # Haploid with a non-ref allele
        if ploidy == 1 and len(non_ref) == 1:
            return "hemizygous"

        # All same allele and it's non-ref
        unique_called = set(called)
        if len(unique_called) == 1 and "0" not in unique_called:
            return "homozygous_alt"

        # Diploid with one ref and one alt
        if ploidy == 2 and len(set(alleles)) == 2 and "0" in alleles:
            return "heterozygous"

        # More than 2 distinct alleles across the genotype
        if len(set(alleles)) > 2:
            return "multi_allelic"

        # Diploid het (e.g. 1/2 — two different non-ref)
        if ploidy == 2 and len(unique_called) > 1:
            return "multi_allelic"

        return "unknown"
