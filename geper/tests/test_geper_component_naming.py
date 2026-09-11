"""
The geper tree names ITSELF as the Bij AI variant-interpretation component
(GEPER), not as the whole product.

Human's rule (2026-09-11), applied to this tree 2026-09-12 after a per-site
inventory: "The defect was never the words 'Bij AI' -- it was a component
claiming to be the whole product." And: "a blanket replace across that many
sites would produce new false claims while looking like consistency" -- so
this file pins sites, not patterns:

  (1) in every inventoried file, each "Bij AI" is followed by
      "variant-interpretation component" or is one of the pinned sites below;
      the five product-name sites (the ratified intended-use statement and two
      licence facts true for the whole product) are pinned byte-unchanged;
  (2) the surfaces themselves -- main.py --help, the API's OpenAPI title and
      summaries, serve_api --help and its startup line (a stand-in uvicorn,
      no port opened), the sign-off CLI's help, and the verify_environment
      banner -- name the component;
  (3) the 17 clinician-facing report-text sites are UNCHANGED, and no other
      "Bij AI" appears in their files, until the human rules on that wording.

Code-emitted strings come from geper/component_identity.py.
"""

import argparse
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPORT_TEXT_PINS = {
    "pipeline/conflict_resolution_engine.py": (
        'conflict_type="Bij AI classification disagrees with an expert-panel/practice-guideline ClinVar classification",',
        'evidence_b={"source": "Bij AI ACMG engine", "statement": f"classification: \'{acmg_classification}\'."},',
        "\"This variant's Bij AI classification must be manually reviewed before clinical use. Bij AI's \"",
        "\"raise that Bij AI's primary-evidence-based classification may be missing something ClinVar's \"",
        '"record, which is why this is Bij AI\'s highest-severity reviewer flag."',
        'conflict_type="Bij AI classification disagrees with a curated (non-expert-panel) ClinVar classification",',
        'evidence_b={"source": "Bij AI ACMG engine", "statement": f"classification: \'{acmg_classification}\'."},',
        "\"This variant's Bij AI classification should be manually reviewed before clinical use. Bij AI's \"",
        '"meet the expert-panel/practice-guideline bar for Bij AI\'s highest-severity flag."',
        'resolution="ACMG classification is unchanged by this conflict. Bij AI\'s ACMG rule engine treats ClinVar as a cross-reference, not a direct classification input (see acmg_evaluation.clinvar_crossreference), so a pathogenic ClinVar assertion in a gene with weak ClinGen validity does not by itself alter the ACMG result -- but this combination warrants manual review before clinical use.",',
        'clinical_sources.append("Bij AI ACMG engine")',
        'resolution_rationale="Bij AI has no directional (pathogenic/benign) signal from either BLAST or Ensembl to compare against other evidence, so a genuine conflict cannot be detected here without fabricating one.",',
    ),
    "pipeline/acmg_rules.py": (
        '"Bij AI does not implement that mtDNA-specific version, so PVS1 is not evaluated for this "',
        'f"the 28 standard ACMG/AMP criteria, Bij AI never evaluates {len(_NEVER_INTEGRATED_ACMG_CODES)} "',
        '"Bij AI actually evaluated for this compartment -- it is not a complete, mtDNA-specification-"',
    ),
    "pipeline/explainability_engine.py": (
        "f\"Bij AI classified {locus}{gene_clause} as '{classification or 'not classified'}' \"",
    ),
    "pipeline/prioritization_engine.py": (
        '"✗ Review priority floored at Critical: Bij AI\'s classification disagrees with an "',
    ),
}
_PRODUCT_NAME_PINS = {
    "pipeline/orchestrator.py": (
        '"reason": "IndiGenomes retired from Bij AI\'s active query path (commercial-use licensing "',
    ),
    "pipeline/functional_evidence/mavedb_provider.py": (
        'f"Bij AI\'s confirmed commercial-use-safe set {sorted(_COMMERCIAL_SAFE_LICENSE_SHORT_NAMES)} "',
    ),
    "pipeline/explainability_engine.py": (
        '"Bij AI is a variant prioritisation system that assists qualified clinicians and pathologists; "',
    ),
    "README.md": (
        "Bij AI is a variant prioritisation system built on pretrained machine",
        "clinical use, and Bij AI does not independently provide final clinical",
    ),
}


# The geper-side names, written out here rather than imported, so a missing or
# changed constant is a test failure rather than a collection error.
_COMPONENT_NAME = "Bij AI variant-interpretation component (GEPER)"
_SHORT_NAME = "GEPER"

_GEPER_ROOT = Path(__file__).resolve().parents[1]

# Files whose "Bij AI" sites were inventoried and scoped (2026-09-12).
_SCOPED_FILES = (
    "main.py",
    "api/__init__.py",
    "api/main.py",
    "api/submission_store.py",
    "api/submission_worker.py",
    "api/exception_retry_worker.py",
    "serve_api.py",
    "review/cli.py",
    "review/signoff.py",
    "verify_environment.py",
    "pipeline/orchestrator.py",
    "pipeline/functional_evidence/mavedb_provider.py",
    "pipeline/explainability_engine.py",
    "README.md",
    "GEPER_Colab.ipynb",
)
_ALLOWED_CONTINUATION = "variant-interpretation component"
# "Bij AI" naming neither component -- the whole-product claim.
_BARE = re.compile(r"Bij AI(?!\s+(?:variant-interpretation|sequencing-analysis) component)")


def _lines(rel):
    return (_GEPER_ROOT / rel).read_text(encoding="utf-8").splitlines()


def _pinned(rel):
    return set(_PRODUCT_NAME_PINS.get(rel, ())) | set(_REPORT_TEXT_PINS.get(rel, ()))


# ── (1) per-site guard ────────────────────────────────────────────────────────


class TestEveryScopedSiteNamesTheComponent(unittest.TestCase):
    def test_every_bij_ai_is_the_component_name_or_a_pinned_site(self):
        offenders = []
        for rel in _SCOPED_FILES:
            lines = _lines(rel)
            pinned = _pinned(rel)
            for i, line in enumerate(lines):
                if "Bij AI" not in line or line.strip() in pinned:
                    continue
                # Join the next line: a wrapped name is still the name.
                window = " ".join((line + " " + (lines[i + 1] if i + 1 < len(lines) else "")).split())
                for m in re.finditer(r"Bij AI", window[: len(" ".join(line.split()))]):
                    if not window[m.end() :].lstrip().startswith(_ALLOWED_CONTINUATION):
                        offenders.append(f"{rel}:{i + 1}: {line.strip()[:110]}")
        self.assertEqual(offenders, [])

    def test_the_five_product_name_sites_are_byte_unchanged(self):
        for rel, pins in _PRODUCT_NAME_PINS.items():
            stripped = [line.strip() for line in _lines(rel)]
            for pin in pins:
                with self.subTest(rel=rel, pin=pin[:60]):
                    self.assertEqual(stripped.count(pin), 1)

    def test_the_constant_module_holds_the_ruled_names(self):
        import component_identity

        self.assertEqual(component_identity.COMPONENT_NAME, _COMPONENT_NAME)
        self.assertEqual(component_identity.SHORT_NAME, _SHORT_NAME)


# ── (2) what the surfaces actually print ──────────────────────────────────────


def _run(args, extra_path=()):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("GEPER_DEV_INSECURE", "1")
    env["PYTHONPATH"] = os.pathsep.join([*map(str, extra_path), str(_GEPER_ROOT), str(_GEPER_ROOT.parent)])
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(_GEPER_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _flat(text):
    return " ".join(text.split())


class TestSurfacesNameTheComponent(unittest.TestCase):
    def test_main_help_names_the_component(self):
        from main import build_arg_parser

        text = _flat(build_arg_parser().format_help())
        self.assertIn(f"{_COMPONENT_NAME} - Genetic Evaluation & Prediction Engine", text)
        self.assertIn(f"{_SHORT_NAME} skips variants already", text)
        self.assertEqual(_BARE.findall(text), [])

    def test_api_openapi_title_and_summaries_name_the_component(self):
        from api.main import app

        spec = app.openapi()
        self.assertEqual(spec["info"]["title"], f"{_COMPONENT_NAME} Structures API")
        post = spec["paths"]["/interpretations"]["post"]
        self.assertEqual(post["summary"], f"Submit a VCF for interpretation by the {_COMPONENT_NAME}")
        self.assertIn(_COMPONENT_NAME, post.get("description", ""))
        summaries = [op.get("summary", "") for path in spec["paths"].values() for op in path.values()]
        self.assertIn(f"Get {_SHORT_NAME} interpretation status", summaries)
        self.assertEqual(_BARE.findall(_flat(str(spec["info"]) + " " + " ".join(summaries))), [])

    def test_serve_api_help_names_the_component(self):
        code, out, err = _run([str(_GEPER_ROOT / "serve_api.py"), "--help"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{_COMPONENT_NAME} structures API server", _flat(out))

    def test_serve_api_startup_line_names_the_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "uvicorn.py").write_text(
                "def run(app, **kw):\n    print('[uvicorn stand-in] app=' + str(app))\n", encoding="utf-8"
            )
            code, out, err = _run([str(_GEPER_ROOT / "serve_api.py")], extra_path=[tmp])
        self.assertEqual(code, 0, err)
        self.assertIn("[uvicorn stand-in]", out)  # nothing was served, no port opened
        self.assertIn(f"Starting the {_COMPONENT_NAME} Structures API on http://127.0.0.1:", out)

    def test_review_cli_help_names_the_component(self):
        from review.cli import build_arg_parser

        parser = build_arg_parser()
        texts = [parser.format_help()]
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                texts += [sub.format_help() for sub in action.choices.values()]
                texts += [a.help or "" for a in action._choices_actions]
        flat = _flat(" ".join(texts))
        self.assertIn(f"Clinician review workflow for {_COMPONENT_NAME} runs", flat)
        self.assertIn(f"An existing {_SHORT_NAME} --output-dir", flat)
        self.assertEqual(_BARE.findall(flat), [])

    def test_verify_environment_banner_names_the_component(self):
        import verify_environment

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            verify_environment.print_human(verify_environment.Report())
        self.assertIn(f"{_COMPONENT_NAME} Environment Verification", buf.getvalue())
        self.assertEqual(_BARE.findall(buf.getvalue()), [])


# ── (3) the 17 clinician-facing report-text sites: UNCHANGED until ruled ──────


class TestReportTextSitesAwaitTheirRuling(unittest.TestCase):
    """These strings reach clinical reports. Their wording goes to the human
    first; until then they must stay exactly as they are -- no early edit,
    and no new "Bij AI" slipped into these files."""

    def test_each_report_text_site_is_byte_unchanged(self):
        for rel, pins in _REPORT_TEXT_PINS.items():
            stripped = [line.strip() for line in _lines(rel)]
            for pin in pins:
                with self.subTest(rel=rel, pin=pin[:60]):
                    self.assertIn(pin, stripped)

    def test_no_other_bij_ai_in_the_report_text_files(self):
        for rel in _REPORT_TEXT_PINS:
            found = {line.strip() for line in _lines(rel) if "Bij AI" in line}
            with self.subTest(rel=rel):
                self.assertEqual(found, _pinned(rel))


if __name__ == "__main__":
    unittest.main()
