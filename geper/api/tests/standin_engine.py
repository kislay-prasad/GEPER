"""
api/tests/standin_engine.py
───────────────────────────
A STAND-IN for `geper/main.py`, for tests of the processing hand-off. It is NOT the genomic pipeline: it runs no
model, reads no database and makes no biological claim. It exists so that the interpretation worker can be driven
through its real subprocess path (`spawn_tracked`) and its real success / failure handling, with a fast engine whose
behaviour a test chooses.

WHAT IT HONOURS, AND HOW THAT IS KEPT HONEST
  * Command line. `geper/main.py` cannot be imported here (it exits on Python outside 3.11 to 3.12 and imports the
    whole pipeline), so this script builds its argument parser from the SYNTAX TREE of the real
    `build_arg_parser()`: the same flags, the same `required` and `store_true` settings. An argument the real CLI
    would reject is rejected here too (argparse exit code 2), and a flag the real CLI added or removed changes this
    parser with it. `test_processing_handoff_contract.py` pins the worker's command line against both this parser and,
    where the interpreter allows, the real one.
  * Output contract. As `geper/main.py`: exit 0 on success, with `geper_results.json` under `--output-dir`; exit 1 for a
    pipeline error; exit 130 for an interrupt. The worker reads exit code and files, never stdout.

BEHAVIOUR is chosen with environment variables (inherited through `spawn_tracked`):
  STANDIN_MODE  success (default) | exit1 | exit130 | no_output | malformed_json | empty_object | hang | slow |
                rendezvous | fail_once | partial_fail | incomplete | count_mismatch | claims_review | wrong_vcf |
                variants_not_a_list
  STANDIN_LOG   a file; one JSON line per invocation (argv, mode, pid) is appended BEFORE any work, so a test can count
                runs and read the arguments the worker actually passed
  STANDIN_HEARTBEAT   `hang` rewrites this file every 0.1 s, so a test can show the process was really terminated
  STANDIN_RENDEZVOUS_DIR / _COUNT / _TIMEOUT   `rendezvous` writes a marker and waits until COUNT markers exist, or
                TIMEOUT seconds pass: two engines running at the same time see each other, one engine alone just waits
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REAL_MAIN = Path(__file__).resolve().parents[2] / "main.py"


def declared_arguments(main_path: Path = REAL_MAIN) -> List[Dict[str, Any]]:
    """
    The arguments `geper/main.py::build_arg_parser` declares, read from its syntax tree (no import, no execution):
    [{"flags": ["--vcf"], "required": True, "action": None}, ...]. Only literal flags and the literal `required` /
    `action` keywords are read, which is all the CLI uses.
    """
    tree = ast.parse(main_path.read_text(encoding="utf-8"), filename=str(main_path))
    builder = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "build_arg_parser"
    )
    declared: List[Dict[str, Any]] = []
    for node in ast.walk(builder):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            flags = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            keywords = {k.arg: k.value.value for k in node.keywords if isinstance(k.value, ast.Constant)}
            declared.append(
                {"flags": flags, "required": bool(keywords.get("required", False)), "action": keywords.get("action")}
            )
    return declared


def build_parser(main_path: Path = REAL_MAIN) -> argparse.ArgumentParser:
    """A parser accepting exactly the flags the real CLI declares (values are plain strings)."""
    parser = argparse.ArgumentParser(prog="standin_engine", allow_abbrev=False)
    for spec in declared_arguments(main_path):
        kwargs: Dict[str, Any] = {}
        if spec["action"] == "store_true":
            kwargs["action"] = "store_true"
        else:
            kwargs["required"] = spec["required"]
        parser.add_argument(*spec["flags"], **kwargs)
    return parser


def _vcf_variants(vcf_path: str) -> Dict[str, Any]:
    samples: List[str] = []
    variants: List[Dict[str, Any]] = []
    with open(vcf_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#CHROM"):
                samples = line.rstrip("\n").split("\t")[9:]
            elif line and not line.startswith("#"):
                fields = line.rstrip("\n").split("\t")
                variants.append(
                    {
                        "variant": {"chrom": fields[0], "pos": int(fields[1]), "ref": fields[3], "alt": fields[4]},
                        "interpretation_result": {"gene_symbol": "STANDIN"},
                    }
                )
    return {"vcf_samples": samples, "variants": variants}


def _success_document(args: argparse.Namespace) -> Dict[str, Any]:
    parsed = _vcf_variants(args.vcf)
    qc_metrics: Optional[Any] = None
    if getattr(args, "qc_metrics_json", None):
        with open(args.qc_metrics_json, "r", encoding="utf-8") as fh:
            qc_metrics = json.load(fh)
    return {
        "geper_version": "standin",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "input_vcf": args.vcf,
        "assembly": args.assembly,
        "vcf_samples": parsed["vcf_samples"],
        "variant_count": len(parsed["variants"]),
        "variants": parsed["variants"],
        "review_status": "draft",
        "reviewed_by": None,
        "reviewed_at": None,
        # the keys the worker's output validation reads (see report/json_builder.py::JSONResultBuilder.build)
        "input_vcf_absolute_path": os.path.abspath(args.vcf),
        "run_complete": True,
        # what the worker actually passed, for the test to read back from the recorded interpretation
        "standin": {
            "argv": sys.argv[1:],
            "hpo_terms": getattr(args, "hpo_terms", None),
            "qc_metrics": qc_metrics,
        },
    }


def valid_engine_document(vcf_path: str, variants: int = 1) -> Dict[str, Any]:
    """
    A document that satisfies the worker's output contract (`submission_worker.validate_engine_output`) for a run of
    `vcf_path`, for tests that stand in for the engine without launching it. The shape is the real builder's
    (report/json_builder.py::JSONResultBuilder.build): `run_complete` true, `variant_count` equal to the variants
    listed, a draft, and the absolute path of the VCF analysed.
    """
    entries = [
        {
            "variant": {"chrom": "1", "pos": 1000 + n, "ref": "A", "alt": "T"},
            "interpretation_result": {"gene_symbol": "STANDIN"},
        }
        for n in range(variants)
    ]
    return {
        "geper_version": "standin",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "input_vcf": vcf_path,
        "input_vcf_absolute_path": os.path.abspath(vcf_path),
        "assembly": "GRCh38",
        "vcf_samples": [],
        "variant_count": len(entries),
        "variants": entries,
        "review_status": "draft",
        "reviewed_by": None,
        "reviewed_at": None,
        "run_complete": True,
    }


def _write_results(output_dir: str, text: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "geper_results.json"), "w", encoding="utf-8") as fh:
        fh.write(text)


def _log_invocation(mode: str) -> int:
    """Append this invocation to STANDIN_LOG and return how many invocations have been logged, including this one."""
    path = os.environ.get("STANDIN_LOG")
    if not path:
        return 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "mode": mode, "pid": os.getpid()}) + "\n")
    with open(path, "r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _rendezvous() -> None:
    directory = os.environ["STANDIN_RENDEZVOUS_DIR"]
    wanted = int(os.environ.get("STANDIN_RENDEZVOUS_COUNT", "2"))
    deadline = time.monotonic() + float(os.environ.get("STANDIN_RENDEZVOUS_TIMEOUT", "6"))
    os.makedirs(directory, exist_ok=True)
    Path(directory, f"engine-{os.getpid()}").write_text("here", encoding="utf-8")
    while time.monotonic() < deadline and len(os.listdir(directory)) < wanted:
        time.sleep(0.05)


def main() -> int:
    args = build_parser().parse_args()  # an argument the real CLI rejects exits 2 here, as there
    mode = os.environ.get("STANDIN_MODE", "success")
    invocations = _log_invocation(mode)
    if mode == "fail_once":
        mode = "exit1" if invocations == 1 else "success"
    if mode == "exit1":
        sys.stderr.write("PipelineError: stand-in engine failure (exit 1)\n")
        return 1
    if mode == "exit130":
        sys.stderr.write("Interrupted by user.\n")
        return 130
    if mode == "no_output":
        return 0
    if mode == "malformed_json":
        _write_results(args.output_dir, '{"variants": [')
        return 0
    if mode == "empty_object":
        _write_results(args.output_dir, "{}")
        return 0
    if mode in ("partial_fail", "incomplete", "count_mismatch", "claims_review", "wrong_vcf", "variants_not_a_list"):
        document = _success_document(args)
        if mode in ("partial_fail", "incomplete"):
            document["run_complete"] = False  # what the engine's periodic checkpoint looks like
            document["variants"] = document["variants"][:1]
            document["variant_count"] = len(document["variants"])
            document["partial_marker"] = "written-by-an-attempt-that-did-not-finish"
        elif mode == "count_mismatch":
            document["variant_count"] = document["variant_count"] + 5
        elif mode == "claims_review":
            document["review_status"] = "reviewed"
            document["reviewed_by"] = "Dr. Somebody"
        elif mode == "wrong_vcf":
            document["input_vcf_absolute_path"] = os.path.abspath(args.vcf) + ".other"
        elif mode == "variants_not_a_list":
            document["variants"] = {"not": "a list"}
        _write_results(args.output_dir, json.dumps(document))
        if mode == "partial_fail":
            sys.stderr.write("PipelineError: stand-in engine failed after writing a checkpoint\n")
            return 1
        return 0
    if mode == "hang":
        # a heartbeat a test can watch: it stops changing the moment this process is really gone
        heartbeat = os.environ.get("STANDIN_HEARTBEAT")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if heartbeat:
                Path(heartbeat).write_text(repr(time.time()), encoding="utf-8")
            time.sleep(0.1)
        return 0
    if mode == "slow":
        time.sleep(1.0)
    if mode == "rendezvous":
        _rendezvous()
    _write_results(args.output_dir, json.dumps(_success_document(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
