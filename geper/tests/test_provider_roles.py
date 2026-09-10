"""
The provider role table must agree with the code, in BOTH directions.

`pipeline/provider_roles.py` is a DECLARATION, not a mechanism: nothing
constructs from it and no production module imports it. That is what makes its
behaviour-change risk zero BY CONSTRUCTION rather than by care -- but it also
means nothing would notice if it went stale. These tests are the only thing
that keeps it honest, and they read the code AS SOURCE (ast), never by
importing and inspecting objects, so a provider whose module cannot be
imported in a bare test environment is still checked.

BOTH DIRECTIONS, ALWAYS. "Every constructed client has a record" catches a
provider added without registration. "Every record names something real"
catches a record left behind when code moved. A table checked in only one
direction rots in the other.

*** AND ONE TEST HERE IS ABOUT THE DOCSTRING, WHICH IS NOT DECORATION. *** The
table's own docstring has to keep saying that it CANNOT answer "what does the
engine read". Someone will eventually use this artefact to answer exactly that
question; if that warning is ever edited out, the artefact becomes actively
misleading, so the warning is a tested property like any other.
"""

import ast
import os
import unittest

from pipeline.provider_roles import PROVIDER_ROLES, ProviderRole, keys_with_role

_GEPER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse(*relative_parts: str) -> ast.Module:
    with open(os.path.join(_GEPER, *relative_parts), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _orchestrator_init_attributes() -> set:
    """Every `self.X = ...` assigned in `GeperPipeline.__init__`, by AST."""
    tree = _parse("pipeline", "orchestrator.py")
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GeperPipeline":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    for sub in ast.walk(item):
                        if isinstance(sub, ast.Assign):
                            for target in sub.targets:
                                if (
                                    isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name)
                                    and target.value.id == "self"
                                ):
                                    found.add(target.attr)
    return found


def _evaluate_result_parameters() -> set:
    """`ACMGClassifier.evaluate`'s keyword-only `*_result` parameters, by AST."""
    tree = _parse("pipeline", "acmg_rules.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate":
            names = {arg.arg for arg in node.args.kwonlyargs}
            if any(n.endswith("_result") for n in names):
                return {n[: -len("_result")] for n in names if n.endswith("_result")}
    raise AssertionError("could not locate evaluate()'s keyword-only parameters")


def _raw_evidence_bundle_fields() -> set:
    """`RawEvidenceBundle`'s annotated field names, by AST."""
    tree = _parse("pipeline", "stage_schemas.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RawEvidenceBundle":
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    raise AssertionError("could not locate RawEvidenceBundle")


class TableAgreesWithTheConstructorTest(unittest.TestCase):
    def test_every_client_attribute_in_the_constructor_has_a_record(self):
        """A 17th provider added without a record must fail here, loudly. This
        is the assertion that is IMPOSSIBLE today: without the table, "every
        provider" is not a computable set, so nothing can quantify over it."""
        declared = {r.attribute for r in PROVIDER_ROLES if r.attribute}
        constructed = {a for a in _orchestrator_init_attributes() if a.endswith("_client")}
        self.assertEqual(
            constructed - declared,
            set(),
            "a *_client is constructed in GeperPipeline.__init__ with no entry in PROVIDER_ROLES",
        )

    def test_every_record_that_claims_construction_names_a_real_attribute(self):
        """The other direction: a record left behind after code moved."""
        constructed_attrs = _orchestrator_init_attributes()
        for record in PROVIDER_ROLES:
            if ProviderRole.CONSTRUCTED_IN_ORCHESTRATOR in record.roles:
                with self.subTest(key=record.key):
                    self.assertIn(
                        record.attribute,
                        constructed_attrs,
                        f"{record.key} claims construction but self.{record.attribute} "
                        "is not assigned in GeperPipeline.__init__",
                    )


class TableAgreesWithTheConsumersTest(unittest.TestCase):
    def test_reaches_evaluate_matches_evaluates_own_signature(self):
        self.assertEqual(
            set(keys_with_role(ProviderRole.REACHES_EVALUATE)),
            _evaluate_result_parameters(),
            "the table and acmg_rules.evaluate() disagree about which results reach the engine",
        )

    def test_raw_evidence_bundle_role_matches_the_bundles_own_fields(self):
        self.assertEqual(
            set(keys_with_role(ProviderRole.IN_RAW_EVIDENCE_BUNDLE)),
            _raw_evidence_bundle_fields(),
            "the table and RawEvidenceBundle disagree about the bundle's members",
        )


class DisagreementsStayVisibleTest(unittest.TestCase):
    """
    *** THESE ASSERT THAT THE SETS STILL DISAGREE. *** They are not describing a
    defect to be fixed; they are holding open a question somebody has to rule
    on. If a future change quietly reconciles the roles, that ruling would be
    made silently by whoever edited the table, which is the outcome this whole
    artefact exists to prevent.
    """

    def test_the_bundle_requires_three_providers_no_criterion_reads(self):
        """
        *** THREE, NOT TWO -- AND THE THIRD IS THE ONE THAT MATTERS. *** This
        assertion originally named only blast and alphafold, the two flagged in
        the report. The table itself surfaced `dbsnp` as a third: it is a
        REQUIRED field of RawEvidenceBundle and no criterion reads it, so the
        report's raw-evidence display demands a source the engine ignores.
        """
        bundle = set(keys_with_role(ProviderRole.IN_RAW_EVIDENCE_BUNDLE))
        read = set(keys_with_role(ProviderRole.READ_BY_A_CRITERION))
        self.assertEqual(
            bundle - read,
            {"blast", "alphafold", "dbsnp"},
            "RawEvidenceBundle requires blast, alphafold and dbsnp, which no criterion reads -- "
            "either correct for its purpose or a defect, and nobody has ruled",
        )

    def test_nine_providers_reach_the_engine_but_not_the_bundle(self):
        reaches = set(keys_with_role(ProviderRole.REACHES_EVALUATE))
        bundle = set(keys_with_role(ProviderRole.IN_RAW_EVIDENCE_BUNDLE))
        self.assertEqual(len(reaches - bundle), 9)

    def test_dbsnp_reaches_evaluate_but_no_criterion_reads_it(self):
        """The defect no container over providers could have caught."""
        self.assertIn("dbsnp", keys_with_role(ProviderRole.REACHES_EVALUATE))
        self.assertNotIn("dbsnp", keys_with_role(ProviderRole.READ_BY_A_CRITERION))


class TheTableIsInertTest(unittest.TestCase):
    def test_no_production_module_imports_the_table(self):
        """
        The moment something imports this at runtime it stops being a
        declaration and starts being a mechanism, and the zero-risk property
        is gone. Tests may import it; production may not.
        """
        offenders = []
        for root, _dirs, files in os.walk(_GEPER):
            if "tests" in root.split(os.sep) or "vendor" in root.split(os.sep):
                continue
            for name in files:
                if not name.endswith(".py") or name == "provider_roles.py":
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        source = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if "provider_roles" in source:
                    offenders.append(os.path.relpath(path, _GEPER))
        self.assertEqual(offenders, [], "provider_roles must stay inert; production code imported it")

    def test_the_docstring_still_says_what_it_cannot_answer(self):
        import pipeline.provider_roles as module

        doc = module.__doc__ or ""
        # Case-insensitive: the docstring shouts its most important sentences,
        # and the warning must survive being re-cased, not just re-worded.
        lowered = doc.lower()
        for phrase in ("consumption", "dbsnp", "cannot"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lowered)

    def test_the_discarded_readings_are_documented(self):
        import pipeline.provider_roles as module

        doc = module.__doc__ or ""
        self.assertIn("DISCARDED READINGS", doc)


if __name__ == "__main__":
    unittest.main()
