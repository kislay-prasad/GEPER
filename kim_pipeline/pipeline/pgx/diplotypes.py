"""
pipeline/pgx/diplotypes.py
──────────────────────────
Hardcoded star-allele definition tables and diplotype→phenotype mappings
for the 10 core PGx genes covered by GEPER.

Data derived from PharmGKB / CPIC guidelines (positions are GRCh38).
No file downloads required — all data is embedded in code.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# ─── Star-allele defining variants ───────────────────────────────────────────
# Format: gene → {star_allele: [(chrom, pos, ref, alt), ...]}
# Positions are GRCh38 / hg38, 1-based VCF coordinates.
# *1 is the reference (no defining variants needed).

STAR_ALLELE_VARIANTS: Dict[str, Dict[str, List[Tuple[str, int, str, str]]]] = {
    "CYP2D6": {
        "*2":  [("22", 42128945, "C", "T")],        # rs16947
        # FIX 7: *3 rs35742686 is a single-base A deletion.
        # GRCh38 VCF representation (left-normalised): REF=CA, ALT=C at pos 42126610
        "*3":  [("22", 42126610, "CA", "C")],       # rs35742686 (frameshift, del A)
        "*4":  [("22", 42128175, "C", "T")],        # rs3892097
        # *5 is a whole-gene deletion — requires CNV analysis (not detectable from SNV VCF)
        "*5":  [],  # CNV — see note below
        # FIX 7: *6 rs5030655 is a single-base T deletion.
        # VCF: REF=AT, ALT=A at pos 42126563
        "*6":  [("22", 42126563, "AT", "A")],       # rs5030655 (del T)
        "*9":  [("22", 42130692, "C", "T")],        # rs5030656
        "*10": [("22", 42130692, "C", "T"), ("22", 42128945, "C", "T")],
        "*17": [("22", 42127941, "C", "T")],        # rs28371706
        "*29": [("22", 42128945, "C", "T"), ("22", 42126611, "A", "G")],
        "*41": [("22", 42128203, "T", "C")],        # rs28371725
    },
    "CYP2C19": {
        "*2":  [("10", 94781858, "G", "A")],        # rs4244285
        "*3":  [("10", 94773863, "G", "A")],        # rs4986893
        "*4":  [("10", 94780653, "A", "G")],        # rs28399504
        "*5":  [("10", 94781944, "C", "T")],        # rs56337013
        "*17": [("10", 94762706, "C", "T")],        # rs12248560
    },
    "CYP2C9": {
        "*2":  [("10", 94942290, "C", "T")],        # rs1799853
        "*3":  [("10", 94981296, "A", "C")],        # rs1057910
        "*5":  [("10", 94981230, "C", "G")],        # rs28371686
        # FIX 7: *6 rs9332131 is a single-base A deletion.
        # VCF: REF=GA, ALT=G at pos 94949280
        "*6":  [("10", 94949280, "GA", "G")],       # rs9332131 (del A)
        "*8":  [("10", 94942255, "G", "A")],        # rs7900194
        "*11": [("10", 94980459, "C", "T")],        # rs28371685
    },
    "TPMT": {
        "*2":  [("6", 18143955, "G", "C")],         # rs1800462
        "*3A": [("6", 18131984, "A", "G"), ("6", 18143955, "G", "A")],
        "*3B": [("6", 18143955, "G", "A")],         # rs1800460
        "*3C": [("6", 18131984, "A", "G")],         # rs1142345
        "*4":  [("6", 18130918, "G", "A")],         # rs1800584
    },
    "DPYD": {
        "*2A": [("1", 97915614, "G", "A")],         # rs3918290 (IVS14+1G>A)
        "*13": [("1", 97981395, "T", "G")],         # rs55886062
        "c.2846A>T": [("1", 97547947, "A", "T")],  # rs67376798
        "c.1236G>A": [("1", 98039419, "C", "T")],  # rs56038477
    },
    "SLCO1B1": {
        "*5":  [("12", 21175421, "T", "C")],        # rs4149056 (Val174Ala)
        "*15": [("12", 21176804, "A", "G"), ("12", 21175421, "T", "C")],
        "*17": [("12", 21176804, "A", "G")],        # rs2306283
    },
    "VKORC1": {
        "-1639G>A": [("16", 31096368, "G", "A")],  # rs9923231
        "1173C>T":  [("16", 31093557, "C", "T")],  # rs9934438
    },
    "G6PD": {
        "G202A": [("X", 154535388, "G", "A")],     # rs1050828 (African A-)
        "A376G": [("X", 154532082, "A", "G")],     # rs1050829
        "Mediterranean": [("X", 154536002, "C", "T")],  # rs5030868
    },
    "CYP3A5": {
        "*3":  [("7", 99672916, "G", "A")],         # rs776746
        "*6":  [("7", 99672916, "G", "T")],         # rare
        # FIX 7: *7 rs41303343 is a single-base insertion (dup A).
        # VCF left-normalised: REF=A, ALT=AA at pos 99673316
        "*7":  [("7", 99673316, "A", "AA")],        # rs41303343 (ins A → frameshift)
    },
    "UGT1A1": {
        "*6":  [("2", 233757013, "G", "A")],        # rs4148323
        "*28": [("2", 233760498, "TA", "TAA")],     # rs8175347 (7/6 TA repeat)
        "*36": [("2", 233760498, "TA", "T")],       # 5 TA repeats
        "*37": [("2", 233760498, "TA", "TAAA")],    # 8 TA repeats
    },
}

# NOTE — CYP2D6 *5 (whole-gene deletion):
# This allele requires copy-number variant (CNV) analysis and CANNOT be detected
# from a standard short-read SNV/indel VCF. It is intentionally left as an empty
# variant list.  The pipeline will report CYP2D6 *5 as "not assessed" and will
# not claim CNV-based allele support.  Users requiring *5 detection should run
# a dedicated CNV caller (e.g., CYP2D6 CNV by XL-PCR, MLPA, or long-read
# sequencing).

# ─── Phenotype mapping: gene → {(allele1, allele2): phenotype} ────────────────
# Sorted tuples used as keys so *1/*4 == *4/*1.

DIPLOTYPE_PHENOTYPES: Dict[str, Dict[Tuple[str, str], str]] = {
    "CYP2D6": {
        ("*1", "*1"): "Normal Metabolizer",
        ("*1", "*2"): "Normal Metabolizer",
        ("*1", "*4"): "Intermediate Metabolizer",
        ("*1", "*5"): "Intermediate Metabolizer",
        ("*1", "*6"): "Intermediate Metabolizer",
        ("*1", "*9"): "Intermediate Metabolizer",
        ("*1", "*10"): "Intermediate Metabolizer",
        ("*1", "*17"): "Normal Metabolizer",
        ("*1", "*41"): "Intermediate Metabolizer",
        ("*2", "*2"): "Normal Metabolizer",
        ("*2", "*4"): "Intermediate Metabolizer",
        ("*4", "*4"): "Poor Metabolizer",
        ("*4", "*5"): "Poor Metabolizer",
        ("*4", "*6"): "Poor Metabolizer",
        ("*5", "*5"): "Poor Metabolizer",
        ("*6", "*6"): "Poor Metabolizer",
        ("*3", "*4"): "Poor Metabolizer",
        ("*3", "*3"): "Poor Metabolizer",
        ("*1xN", "*1"): "Ultrarapid Metabolizer",
        ("*2xN", "*1"): "Ultrarapid Metabolizer",
        ("*10", "*10"): "Poor Metabolizer",
    },
    "CYP2C19": {
        ("*1", "*1"): "Normal Metabolizer",
        ("*1", "*2"): "Intermediate Metabolizer",
        ("*1", "*3"): "Intermediate Metabolizer",
        ("*1", "*17"): "Rapid Metabolizer",
        ("*2", "*2"): "Poor Metabolizer",
        ("*2", "*3"): "Poor Metabolizer",
        ("*3", "*3"): "Poor Metabolizer",
        ("*17", "*17"): "Ultrarapid Metabolizer",
        ("*1", "*4"): "Intermediate Metabolizer",
        ("*2", "*17"): "Intermediate Metabolizer",
    },
    "CYP2C9": {
        ("*1", "*1"): "Normal Metabolizer",
        ("*1", "*2"): "Intermediate Metabolizer",
        ("*1", "*3"): "Intermediate Metabolizer",
        ("*2", "*2"): "Intermediate Metabolizer",
        ("*2", "*3"): "Poor Metabolizer",
        ("*3", "*3"): "Poor Metabolizer",
        ("*1", "*5"): "Intermediate Metabolizer",
        ("*1", "*6"): "Intermediate Metabolizer",
        ("*1", "*8"): "Intermediate Metabolizer",
        ("*1", "*11"): "Intermediate Metabolizer",
    },
    "TPMT": {
        ("*1", "*1"): "Normal Metabolizer",
        ("*1", "*2"): "Intermediate Metabolizer",
        ("*1", "*3A"): "Intermediate Metabolizer",
        ("*1", "*3B"): "Intermediate Metabolizer",
        ("*1", "*3C"): "Intermediate Metabolizer",
        ("*2", "*2"): "Poor Metabolizer",
        ("*2", "*3A"): "Poor Metabolizer",
        ("*3A", "*3A"): "Poor Metabolizer",
        ("*3A", "*3C"): "Poor Metabolizer",
        ("*3C", "*3C"): "Poor Metabolizer",
        ("*1", "*4"): "Intermediate Metabolizer",
    },
    "DPYD": {
        ("*1", "*1"): "Normal Metabolizer",
        ("*1", "*2A"): "Intermediate Metabolizer",
        ("*2A", "*2A"): "Poor Metabolizer",
        ("*1", "*13"): "Intermediate Metabolizer",
        ("*13", "*13"): "Poor Metabolizer",
        ("*1", "c.2846A>T"): "Intermediate Metabolizer",
        ("*1", "c.1236G>A"): "Intermediate Metabolizer",
        ("c.2846A>T", "c.2846A>T"): "Poor Metabolizer",
    },
    "SLCO1B1": {
        ("*1a", "*1a"): "Normal Function",
        ("*1a", "*5"): "Decreased Function",
        ("*1a", "*15"): "Decreased Function",
        ("*5", "*5"): "Poor Function",
        ("*5", "*15"): "Poor Function",
        ("*15", "*15"): "Poor Function",
        ("*1", "*1"): "Normal Function",
        ("*1", "*5"): "Decreased Function",
        ("*1", "*15"): "Decreased Function",
    },
    "VKORC1": {
        ("GG", "GG"): "Normal Sensitivity",
        ("GG", "GA"): "Intermediate Sensitivity",
        ("GA", "GA"): "Intermediate Sensitivity",
        ("GA", "AA"): "High Sensitivity",
        ("AA", "AA"): "High Sensitivity",
        ("-1639GG", "-1639GG"): "Normal Sensitivity",
        ("-1639GA", "-1639GA"): "Intermediate Sensitivity",
        ("-1639AA", "-1639AA"): "High Sensitivity",
        ("*1", "*1"): "Normal Sensitivity",
        ("*1", "*2"): "Intermediate Sensitivity",
        ("*2", "*2"): "High Sensitivity",
    },
    "G6PD": {
        ("B", "B"): "Normal",
        ("A", "B"): "Normal",
        ("A-", "B"): "Deficient",
        ("A-", "A-"): "Deficient",
        ("Mediterranean", "B"): "Deficient",
        ("Mediterranean", "Mediterranean"): "Deficient",
        ("*1", "*1"): "Normal",
        ("*1", "G202A"): "Deficient",
        ("G202A", "G202A"): "Deficient",
    },
    "CYP3A5": {
        ("*1", "*1"): "Normal Metabolizer",
        ("*1", "*3"): "Intermediate Metabolizer",
        ("*3", "*3"): "Poor Metabolizer",
        ("*1", "*6"): "Intermediate Metabolizer",
        ("*3", "*6"): "Poor Metabolizer",
        ("*6", "*6"): "Poor Metabolizer",
        ("*1", "*7"): "Intermediate Metabolizer",
        ("*3", "*7"): "Poor Metabolizer",
    },
    "UGT1A1": {
        ("*1", "*1"): "Normal Metabolizer",
        ("*1", "*28"): "Intermediate Metabolizer",
        ("*28", "*28"): "Poor Metabolizer",
        ("*1", "*6"): "Intermediate Metabolizer",
        ("*6", "*6"): "Poor Metabolizer",
        ("*1", "*37"): "Intermediate Metabolizer",
        ("*28", "*37"): "Poor Metabolizer",
        ("*1", "*36"): "Rapid Metabolizer",
        ("*36", "*36"): "Ultrarapid Metabolizer",
    },
}

# ─── Drug implications: gene → phenotype → list of {drug, implication, guideline} ──

DRUG_IMPLICATIONS: Dict[str, Dict[str, List[Dict[str, str]]]] = {
    "CYP2D6": {
        "Poor Metabolizer": [
            {"drug": "codeine", "implication": "Avoid — risk of toxicity (ultra-low conversion to morphine)", "guideline": "CPIC"},
            {"drug": "tramadol", "implication": "Avoid — use alternative opioid", "guideline": "CPIC"},
            {"drug": "amitriptyline", "implication": "Reduce dose by 50% or choose alternative", "guideline": "CPIC"},
            {"drug": "nortriptyline", "implication": "Reduce dose by 50%", "guideline": "CPIC"},
            {"drug": "fluoxetine", "implication": "Initiate with lowest available dose", "guideline": "DPWG"},
        ],
        "Intermediate Metabolizer": [
            {"drug": "codeine", "implication": "Use with caution — monitor for side effects", "guideline": "CPIC"},
            {"drug": "amitriptyline", "implication": "Reduce dose by 25%", "guideline": "CPIC"},
            {"drug": "atomoxetine", "implication": "Initiate at 25% of normal dose", "guideline": "CPIC"},
        ],
        "Ultrarapid Metabolizer": [
            {"drug": "codeine", "implication": "Avoid — risk of toxicity (excess morphine)", "guideline": "CPIC"},
            {"drug": "tramadol", "implication": "Avoid — excessive opioid effect", "guideline": "CPIC"},
            {"drug": "antidepressants (TCA)", "implication": "May need higher doses — monitor", "guideline": "CPIC"},
        ],
        "Normal Metabolizer": [],
    },
    "CYP2C19": {
        "Poor Metabolizer": [
            {"drug": "clopidogrel", "implication": "Avoid — use alternative antiplatelet agent (prasugrel/ticagrelor)", "guideline": "CPIC"},
            {"drug": "voriconazole", "implication": "Reduce dose — monitor plasma levels", "guideline": "CPIC"},
            {"drug": "omeprazole", "implication": "Standard dose likely sufficient (increased drug exposure)", "guideline": "CPIC"},
            {"drug": "escitalopram", "implication": "Reduce dose by 50%", "guideline": "CPIC"},
            {"drug": "amitriptyline", "implication": "Reduce dose by 50%", "guideline": "CPIC"},
        ],
        "Intermediate Metabolizer": [
            {"drug": "clopidogrel", "implication": "Consider alternative antiplatelet agent", "guideline": "CPIC"},
            {"drug": "voriconazole", "implication": "Monitor therapeutic drug levels", "guideline": "CPIC"},
        ],
        "Ultrarapid Metabolizer": [
            {"drug": "clopidogrel", "implication": "Standard dosing appropriate", "guideline": "CPIC"},
            {"drug": "voriconazole", "implication": "May need increased dose — monitor levels", "guideline": "CPIC"},
            {"drug": "PPIs (omeprazole)", "implication": "Reduced efficacy — consider higher dose", "guideline": "CPIC"},
        ],
        "Rapid Metabolizer": [
            {"drug": "PPIs (omeprazole)", "implication": "Slightly reduced efficacy — may need dose adjustment", "guideline": "CPIC"},
        ],
        "Normal Metabolizer": [],
    },
    "CYP2C9": {
        "Poor Metabolizer": [
            {"drug": "warfarin", "implication": "Reduce dose 50-75%; risk of bleeding", "guideline": "CPIC"},
            {"drug": "phenytoin", "implication": "Reduce initial dose; monitor levels closely", "guideline": "CPIC"},
            {"drug": "NSAIDs (celecoxib)", "implication": "Reduce dose by 50% or use alternative", "guideline": "CPIC"},
            {"drug": "fluvastatin", "implication": "Reduce dose; increased exposure", "guideline": "DPWG"},
        ],
        "Intermediate Metabolizer": [
            {"drug": "warfarin", "implication": "Reduce initial dose; careful INR monitoring", "guideline": "CPIC"},
            {"drug": "phenytoin", "implication": "Reduce initial dose by 25%; monitor levels", "guideline": "CPIC"},
            {"drug": "NSAIDs (celecoxib)", "implication": "Use lowest effective dose", "guideline": "CPIC"},
        ],
        "Normal Metabolizer": [],
    },
    "TPMT": {
        "Poor Metabolizer": [
            {"drug": "azathioprine", "implication": "Extreme risk of myelosuppression — reduce dose 10-fold or use alternative", "guideline": "CPIC"},
            {"drug": "6-mercaptopurine", "implication": "Reduce dose to 10% of normal; monitor blood counts", "guideline": "CPIC"},
            {"drug": "thioguanine", "implication": "Reduce dose substantially; alternative recommended", "guideline": "CPIC"},
        ],
        "Intermediate Metabolizer": [
            {"drug": "azathioprine", "implication": "Reduce dose by 30-70%; monitor blood counts", "guideline": "CPIC"},
            {"drug": "6-mercaptopurine", "implication": "Reduce dose by 30-50%", "guideline": "CPIC"},
        ],
        "Normal Metabolizer": [],
    },
    "DPYD": {
        "Poor Metabolizer": [
            {"drug": "fluorouracil (5-FU)", "implication": "Avoid — risk of severe/fatal toxicity", "guideline": "CPIC"},
            {"drug": "capecitabine", "implication": "Avoid — risk of severe/fatal toxicity", "guideline": "CPIC"},
            {"drug": "tegafur", "implication": "Avoid", "guideline": "DPWG"},
        ],
        "Intermediate Metabolizer": [
            {"drug": "fluorouracil (5-FU)", "implication": "Reduce dose by 25-50%; monitor for toxicity", "guideline": "CPIC"},
            {"drug": "capecitabine", "implication": "Reduce dose by 25-50%", "guideline": "CPIC"},
        ],
        "Normal Metabolizer": [],
    },
    "SLCO1B1": {
        "Poor Function": [
            {"drug": "simvastatin", "implication": "Avoid or use lowest dose — increased risk of myopathy", "guideline": "CPIC"},
            {"drug": "lovastatin", "implication": "Use alternative statin", "guideline": "CPIC"},
            {"drug": "atorvastatin", "implication": "Use lower dose; monitor for myopathy", "guideline": "CPIC"},
        ],
        "Decreased Function": [
            {"drug": "simvastatin", "implication": "Use lowest available dose; consider alternative statin", "guideline": "CPIC"},
        ],
        "Normal Function": [],
    },
    "VKORC1": {
        "High Sensitivity": [
            {"drug": "warfarin", "implication": "Lower initial dose required; careful INR monitoring", "guideline": "CPIC"},
        ],
        "Intermediate Sensitivity": [
            {"drug": "warfarin", "implication": "Slightly lower initial dose; monitor INR", "guideline": "CPIC"},
        ],
        "Normal Sensitivity": [],
    },
    "G6PD": {
        "Deficient": [
            {"drug": "rasburicase", "implication": "Contraindicated — risk of severe hemolysis", "guideline": "CPIC"},
            {"drug": "primaquine", "implication": "Contraindicated — risk of severe hemolysis", "guideline": "CPIC"},
            {"drug": "dapsone", "implication": "Avoid — high risk of hemolytic anemia", "guideline": "CPIC"},
            {"drug": "nitrofurantoin", "implication": "Avoid — risk of hemolytic anemia", "guideline": "CPIC"},
        ],
        "Normal": [],
    },
    "CYP3A5": {
        "Poor Metabolizer": [
            {"drug": "tacrolimus", "implication": "Reduce initial dose; monitor trough levels", "guideline": "CPIC"},
        ],
        "Intermediate Metabolizer": [
            {"drug": "tacrolimus", "implication": "Higher dose may be required; monitor closely", "guideline": "CPIC"},
        ],
        "Normal Metabolizer": [
            {"drug": "tacrolimus", "implication": "Standard dosing; monitor trough levels", "guideline": "CPIC"},
        ],
    },
    "UGT1A1": {
        "Poor Metabolizer": [
            {"drug": "irinotecan", "implication": "Reduce dose by at least 1 level; risk of severe neutropenia", "guideline": "CPIC"},
            {"drug": "atazanavir", "implication": "Increased bilirubin expected; not dose-limiting", "guideline": "CPIC"},
        ],
        "Intermediate Metabolizer": [
            {"drug": "irinotecan", "implication": "Use with caution at higher doses", "guideline": "CPIC"},
        ],
        "Normal Metabolizer": [],
        "Ultrarapid Metabolizer": [],
        "Rapid Metabolizer": [],
    },
}

# ─── Activity scores (gene → {phenotype: float}) ─────────────────────────────
# Based on CPIC activity score definitions where applicable.

ACTIVITY_SCORES: Dict[str, Dict[str, Optional[float]]] = {
    "CYP2D6": {
        "Ultrarapid Metabolizer": 3.0,
        "Normal Metabolizer": 2.0,
        "Intermediate Metabolizer": 1.0,
        "Poor Metabolizer": 0.0,
    },
    "CYP2C19": {
        "Ultrarapid Metabolizer": 3.0,
        "Rapid Metabolizer": 2.5,
        "Normal Metabolizer": 2.0,
        "Intermediate Metabolizer": 1.0,
        "Poor Metabolizer": 0.0,
    },
    "CYP2C9": {
        "Normal Metabolizer": 2.0,
        "Intermediate Metabolizer": 1.0,
        "Poor Metabolizer": 0.0,
    },
    "TPMT": {
        "Normal Metabolizer": 2.0,
        "Intermediate Metabolizer": 1.0,
        "Poor Metabolizer": 0.0,
    },
    "DPYD": {
        "Normal Metabolizer": 2.0,
        "Intermediate Metabolizer": 1.0,
        "Poor Metabolizer": 0.0,
    },
    "SLCO1B1": {
        "Normal Function": 2.0,
        "Decreased Function": 1.0,
        "Poor Function": 0.0,
    },
    "VKORC1": {
        "Normal Sensitivity": None,
        "Intermediate Sensitivity": None,
        "High Sensitivity": None,
    },
    "G6PD": {
        "Normal": None,
        "Deficient": None,
    },
    "CYP3A5": {
        "Normal Metabolizer": 2.0,
        "Intermediate Metabolizer": 1.0,
        "Poor Metabolizer": 0.0,
    },
    "UGT1A1": {
        "Ultrarapid Metabolizer": 3.0,
        "Rapid Metabolizer": 2.5,
        "Normal Metabolizer": 2.0,
        "Intermediate Metabolizer": 1.0,
        "Poor Metabolizer": 0.0,
    },
}

# ─── Default phenotype for *1/*1 (no defining variants detected) ──────────────

DEFAULT_PHENOTYPE: Dict[str, str] = {
    "CYP2D6":  "Normal Metabolizer",
    "CYP2C19": "Normal Metabolizer",
    "CYP2C9":  "Normal Metabolizer",
    "TPMT":    "Normal Metabolizer",
    "DPYD":    "Normal Metabolizer",
    "SLCO1B1": "Normal Function",
    "VKORC1":  "Normal Sensitivity",
    "G6PD":    "Normal",
    "CYP3A5":  "Poor Metabolizer",   # *3/*3 is actually majority phenotype
    "UGT1A1":  "Normal Metabolizer",
}

# ─── Evidence levels ──────────────────────────────────────────────────────────

EVIDENCE_LEVELS: Dict[str, str] = {
    "CYP2D6":  "1A",
    "CYP2C19": "1A",
    "CYP2C9":  "1A",
    "TPMT":    "1A",
    "DPYD":    "1A",
    "SLCO1B1": "1A",
    "VKORC1":  "1A",
    "G6PD":    "1A",
    "CYP3A5":  "1A",
    "UGT1A1":  "1A",
}

# All covered genes
ALL_GENES = list(STAR_ALLELE_VARIANTS.keys())
