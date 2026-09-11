"""
The transformers version GEPER DECLARES must be the one the image SHIPS.

Human ruling, 2026-09-11 (card FINDING-THE-SHIPPED-IMAGE-PINS-transformers-
4-56-2-...): "Bring the requirement in line with the image. The image is what
ships, so a requirements file demanding a version the runtime doesn't have is
a claim about the product that the product contradicts."

Until then the Dockerfile's last-wins `pip install "transformers==4.56.2"`
(Enformer needs exactly 4.56.2, Borzoi <5) shipped a version that
geper/requirements.txt (`>=5.12.1,<6.0.0`) rejected, and verify_environment.py
reported `overall: FAIL` inside every image checked (2 of 2).

The Dockerfile is the authority here -- its pin is read out of it, never
restated in this test -- and every other place that DECLARES the version GEPER
runs on is checked against it:
  - geper/requirements.txt           (the requirement itself)
  - geper/verify_environment.py      (what the checker calls PASS)
  - geper/GEPER_Colab.ipynb          (the non-Docker install path)
  - geper/README.md                  (the compatibility matrix that says it
                                      mirrors the two .py/.txt files above)
"""

import importlib.util
import json
import os
import re
import sys
import unittest

_GEPER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_GEPER_ROOT)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _dockerfile_transformers_pin():
    """The version the image ships: the LAST `pip install` of an exact
    transformers pin in the Dockerfile (it is a last-wins override)."""
    text = _read(os.path.join(_REPO_ROOT, "Dockerfile")).replace("\r", "")
    # Join backslash-continued lines so a RUN spanning lines is one instruction.
    text = re.sub(r"\\\n", " ", text)
    pins = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("RUN ") or "pip install" not in stripped:
            continue
        pins.extend(re.findall(r"""["']?\btransformers(?:\[[^\]]*\])?==([0-9][\w.+-]*)["']?""", stripped))
    return pins


class TestTransformersRequirementMatchesShippedImage(unittest.TestCase):
    def setUp(self):
        pins = _dockerfile_transformers_pin()
        self.assertTrue(
            pins, "Dockerfile has no RUN pip install of an exact transformers pin -- nothing to compare against"
        )
        self.shipped = pins[-1]

    def test_dockerfile_pin_is_the_known_value_this_test_was_written_against(self):
        # Sanity for the parser, not the authority: if this fails because the
        # Dockerfile legitimately moved, update the literal; if it fails
        # because the parser found nothing sensible, the parser is broken.
        self.assertEqual(self.shipped, "4.56.2")

    def test_requirements_txt_pins_exactly_what_the_image_ships(self):
        text = _read(os.path.join(_GEPER_ROOT, "requirements.txt"))
        lines = [ln.strip() for ln in text.splitlines() if re.match(r"^\s*transformers\b", ln)]
        self.assertEqual(len(lines), 1, f"expected exactly one transformers requirement line, found {lines}")
        self.assertEqual(
            lines[0].split("#")[0].strip(),
            f"transformers=={self.shipped}",
            "geper/requirements.txt must declare the transformers version the Dockerfile ships",
        )

    def test_verify_environment_calls_the_shipped_version_a_pass(self):
        spec = importlib.util.spec_from_file_location(
            "_verify_environment_under_test", os.path.join(_GEPER_ROOT, "verify_environment.py")
        )
        module = importlib.util.module_from_spec(spec)
        # @dataclass resolves its module through sys.modules at class creation.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        self.assertEqual(module.EXPECTED.get("transformers"), self.shipped)
        # And no leftover range keys a future edit could consult instead.
        self.assertNotIn("transformers_min", module.EXPECTED)
        self.assertNotIn("transformers_max_exclusive", module.EXPECTED)

    def test_colab_notebook_installs_the_shipped_version(self):
        nb = json.loads(_read(os.path.join(_GEPER_ROOT, "GEPER_Colab.ipynb")))
        specs = []
        for cell in nb.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            for line in cell.get("source", []):
                if "pip install" in line:
                    specs.extend(re.findall(r"""['"]?(\btransformers(?:\[[^\]]*\])?[=<>!~][^'"\s]*)""", line))
        self.assertTrue(specs, "the Colab notebook no longer installs transformers at all -- re-check this test")
        for spec_ in specs:
            self.assertEqual(spec_, f"transformers=={self.shipped}")

    def test_readme_compatibility_matrix_states_the_shipped_version(self):
        text = _read(os.path.join(_GEPER_ROOT, "README.md"))
        rows = [ln for ln in text.splitlines() if ln.startswith("| transformers |")]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn(self.shipped, rows[0])
        self.assertNotIn("5.12.1", rows[0])


if __name__ == "__main__":
    unittest.main()
