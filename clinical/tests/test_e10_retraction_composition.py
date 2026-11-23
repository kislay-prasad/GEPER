"""
E10 static guard: every DataAccess method that gates on a delivery state
('approved' / 'released') must also ask whether the report was retracted.

Ported from the unmerged branches w123-e10-matcher and w124-claim-fix
(commits e821c23 and 8a1bfe2, 2026-09-13), which master never received.
Master carries the retraction feature itself (report_retractions,
_read_retraction_row, the banners) but not this enumeration, so nothing
stopped a new delivery gate from forgetting retraction. Pure AST/text
checks over clinical/data_access.py and clinical/retention.py -- no
database is needed.
"""

from __future__ import annotations

import ast
import pathlib
import re

# ─── HUMAN RULING E10 (w122): the enumeration that catches a forgetful reader ─
#
# THE RESIDUAL THIS ANSWERS, STATED HONESTLY BECAUSE IT IS REAL. Under the
# approved retraction model (model 3) the retraction lives in its own table
# beside the report, and NOTHING IN THE DATABASE stops a reader that checks
# `reports.state` and forgets to check for a retraction. The trigger cannot
# help -- there is no row to refuse. The grant cannot help -- reading is
# permitted. A sixth state would have refused a retracted report in every
# existing reader for free, and would have bought that by rewriting a row that
# says a report was approved into one that says it was not.
#
# The human accepted that trade on the condition that the forgetfulness is
# DETECTED. This is the detector, and its design matters more than its
# existence: an enumeration that pinned the CURRENT SET of composing methods
# would pass happily while a new reader forgot. What is pinned instead is the
# RELATIONSHIP -- "gates on the delivery states" implies "composes retraction"
# -- so a method written tomorrow is measured by the same rule.
#
# HOW A "READER" IS IDENTIFIED, and why this shape rather than a grep. It is
# a method defined directly in class DataAccess, in this one file, whose body
# contains an ==, !=, in or not in comparison that NAMES AT LEAST ONE DELIVERY
# STATE as a string constant. The name may appear directly ('approved'), inside
# a literal tuple/list/set, or through a module-level constant bound to such a
# literal. That is the whole rule, and it is deliberately one rule rather than
# a shape per call site.
#
# WHAT THE RULE USED TO BE, AND WHY IT WAS WIDENED (w123). It used to demand
# all three of: an @auditable CALL decorator, a containment operator, and a
# literal sequence holding BOTH states. Measured against a4907d3 with three
# mutant readers inserted into DataAccess, only one of the three was caught:
#   decorated   + `if state in ('approved', 'released')`   CAUGHT
#   decorated   + `if state == 'released'`                 MISSED -- 21 passed
#   undecorated + `if state in ('approved', 'released')`   MISSED -- 21 passed
# Dropping the decorator requirement and admitting equality against a single
# delivery state catches all three. The decorator requirement was the weakest
# of the three anyway: it asserted a convention (every entry point is audited)
# as if it were the definition of a reader, so the cheapest way to escape the
# detector was to write the gate in a helper -- which is where gates normally
# end up.
#
# NEGATION FORMS, AND THE ONE THAT IS DELIBERATELY NOT MATCHED.
#   INCLUDED: `not in ('approved', 'released')` and `!= 'released'`. Those are
#   the canonical shapes in this file -- require_release, retract_report and
#   create_amendment all raise on `state not in (...)`, and _release_report
#   tests `state != 'released'`. A decision taken BY NEGATING a delivery state
#   is still a decision taken on the delivery states.
#   NOT INCLUDED: the complement over NON-delivery states -- `!= 'draft'`,
#   `not in ('draft', 'under_review', 'returned')`. It admits the delivery
#   states by omission, so on paper it is a delivery gate too. In practice it
#   is the shape of every ordinary workflow precondition: place_order tests
#   `orders.state != 'draft'` and has nothing to do with reports at all;
#   submit_for_review and _approve_report gate on ENTRY to the workflow, not on
#   EXIT to a consumer. Matching it would pull three unrelated methods in and
#   each would need an exemption, and a detector that hands out exemptions to
#   stay quiet is how this guarantee dies. It is listed as a blind spot on the
#   test below rather than silently dropped.
#
# HOW COMPOSITION IS IDENTIFIED: the method calls `self._read_retraction_row`,
# the one-row reader a gate asks the retraction question with. Routing every
# COMPOSER through one helper is what makes this question mechanical instead
# of textual. Note what this does and does not establish: it proves the
# QUESTION WAS ASKED, not that the answer was used.
#
# AND NOTE WHAT IT DOES NOT SAY (w124). That helper is not the only code that
# reads `report_retractions` -- its w122 docstring claimed to be and was
# wrong. `_read_retracted` and clinical/retention.py's tombstone cascade each
# run their own SELECT, for reasons recorded at both sites. The property this
# enumeration needs is narrower and is the one that holds: every DELIVERY-STATE
# GATE in DataAccess goes through the helper. test_the_retraction_reader_
# docstring_names_every_reader_of_the_table below measures the wider set so
# the narrower claim cannot quietly turn back into a census.

_DATA_ACCESS = pathlib.Path(__file__).resolve().parent.parent / "data_access.py"

DELIVERY_STATES = frozenset({"approved", "released"})

# The one-row retraction read. A composer calls it; a forgetful reader does
# not. (Not the only reader of the table -- see the note above.)
RETRACTION_READER = "_read_retraction_row"

# Deliberately exempt, with a reason as specific as the ones in
# test_w120_audit_resource_id.py's own exemption list. Growing this list needs
# a ruling, not a convenience.
RETRACTION_COMPOSITION_EXEMPT = {
    # HUMAN RULING E4: "a replacement is optional; a retracted report may
    # still be amended." Amendment is the mechanism that keeps traceability
    # from a corrected document back to the one it corrects, so it is
    # DELIBERATELY indifferent to retraction -- forbidding it would push a lab
    # toward issuing an unlinked new report and losing the link. This is the
    # one gate whose correct behaviour is to not care.
    "create_amendment",
    # PULLED INTO SCOPE BY THE w123 WIDENING, listed here rather than left to
    # be discovered, because an exemption nobody wrote down is the same thing
    # as a matcher that never matched.
    #
    # It IS the retraction reader -- the function that turns a retraction row
    # into the banner a report prints -- so requiring it to compose retraction
    # is requiring it to call itself. It reaches the retraction table with its
    # own SELECT rather than through _read_retraction_row -- w122 called that a
    # crack worth fixing; w124 measured it and ruled the other way. The helper
    # is a `_query_one` and this reader must see a SECOND row to refuse it, so
    # the CODE is right and it was the helper's "one place" SENTENCE that was
    # wrong. That sentence is now corrected; this exemption stands on the
    # reader/composer distinction rather than on a defect. Its `== 'approved'` /
    # `== 'released'` comparisons are against retracted_from_state -- the state
    # the report was retracted FROM, stored on the retraction row -- not
    # against reports.state, so it gates nothing that reaches a consumer.
    "_read_retracted",
    # Also pulled in by the widening (undecorated helper, `in ('approved',
    # 'released')`). Its membership test is about the AMENDMENT's state, not
    # the state of the report being read: it picks the latest amendment that
    # has actually been issued. Human ruling E4 again -- a retracted report may
    # still be amended, and the amendment banner must keep naming its
    # amendment. The caller that assembles the report around it is the one that
    # must compose retraction, and get_report does.
    "_read_superseded_by",
    # ADDED WHEN PORTED ONTO MASTER (both appeared after w124, both match the
    # detector's shape without being a delivery gate):
    #
    # Refuses a NEW reviewer claim on a report that is approved or released --
    # the report is sealed and a claim added now would make it look altered.
    # A retracted report is still sealed (retraction leaves `state` untouched by
    # design, model 3), so the retraction answer cannot change this refusal.
    # It hands nothing to a consumer.
    "_validate_claim_inputs",
    # Its `!= "approved"` is against caller_version_approvals -- whether the
    # CALLER VERSION is approved for this record class -- not against a report's
    # state. No report is read or delivered here.
    "validate_call_set",
}


def _module_level_state_constants(tree):
    """
    Module-level names bound to a literal sequence of strings.

    Only module level, and only a literal: a name assigned inside a function,
    or built by a call this walk cannot evaluate, is not resolved and the
    comparison that uses it is simply not matched. Guessing at a value would
    be worse than a stated blind spot.
    """
    constants = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            # frozenset({...}) / set([...]) / tuple(...) around a literal.
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in ("frozenset", "set", "tuple", "list")
                and len(value.args) == 1
            ):
                value = value.args[0]
            if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                continue
            strings = {e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = strings
    return constants


def _names_a_delivery_state(node, constants):
    """
    Does this comparison operand name at least one delivery state?

    Three shapes, and only three: the bare constant ('approved'), a literal
    sequence containing one ({'approved', 'released'}), and a module-level
    name bound to such a literal. Anything else -- an enum member, an f-string,
    a value fetched from a column, a name assigned locally -- is not resolved.
    """
    if isinstance(node, ast.Constant):
        return node.value in DELIVERY_STATES
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(isinstance(e, ast.Constant) and e.value in DELIVERY_STATES for e in node.elts)
    if isinstance(node, ast.Name):
        return bool(constants.get(node.id, frozenset()) & DELIVERY_STATES)
    return False


def _delivery_state_gates(tree):
    """
    Every DataAccess method whose body compares something against a delivery
    state, with whether it composes retraction.

    Decorated or not; ==, !=, in or not in; one state or both. See the block
    comment above for why each of those was widened and for the one negation
    form (the complement over NON-delivery states) that is left out.
    """
    constants = _module_level_state_constants(tree)
    found = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            gates = False
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Compare):
                    continue
                for op, comparator in zip(inner.ops, inner.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)):
                        # `x in SEQ`: only the container names the states.
                        if _names_a_delivery_state(comparator, constants):
                            gates = True
                    elif isinstance(op, (ast.Eq, ast.NotEq)):
                        # Either side, so `'approved' == state` is not a hole.
                        if _names_a_delivery_state(comparator, constants) or _names_a_delivery_state(
                            inner.left, constants
                        ):
                            gates = True
            if not gates:
                continue
            composes = any(
                isinstance(inner, ast.Attribute) and inner.attr == RETRACTION_READER for inner in ast.walk(node)
            )
            found[node.name] = composes
    return found


def test_every_delivery_state_gate_composes_retraction():
    """
    THE RELATIONSHIP, not the roster -- within one file, over one shape.

    WHAT IT ENFORCES, EXACTLY. Every method defined directly in class
    DataAccess in clinical/data_access.py whose PYTHON SOURCE contains an
    ==, !=, in or not in comparison naming 'approved' or 'released' as a
    string constant (bare, in a literal sequence, or via a module-level
    constant bound to one) must also call self._read_retraction_row, or be
    named in RETRACTION_COMPOSITION_EXEMPT with a reason.

    WHAT IT CANNOT SEE. This is an AST walk over one Python file, and the
    blind spots are not hypothetical -- the first one is a shape this very
    codebase uses on every query:

      1. SQL STRING GATES, and this is permanent. A gate written as
         `WHERE state IN ('approved', 'released')` -- or `state = 'approved'`,
         or pushed down into a view, a stored function or a trigger -- is a
         string constant to Python. An AST walk sees a str, not a comparison,
         and no amount of widening changes that without becoming a grep over
         SQL text. A delivery path that filters in the database and never
         mentions a state in Python passes this test while forgetting
         retraction entirely.
      2. INDIRECTION IN THE STATE NAME. An enum member (ReportState.APPROVED),
         an f-string, a locally-assigned variable, a value read from another
         row, a set built by a call -- none resolve here. Only bare constants,
         literal sequences and module-level constants bound to literals do.
      3. ANYTHING OUTSIDE DataAccess. Gates in geper/api routes, in
         submission_worker, in a renderer, or in a subclass or mixin, are not
         walked. Only methods in this one class body in this one file are.
      4. COMPOSITION IS A CALL, NOT A USE. Matching self._read_retraction_row
         proves the question was ASKED. A method that calls it and throws the
         answer away passes. Nothing here checks what the caller does with a
         retraction it found.
      5. THE COMPLEMENT FORM IS NOT MATCHED ON PURPOSE. `state != 'draft'`
         admits the delivery states by omission and is not treated as a
         delivery gate; see the block comment above for why, and for the three
         methods that would otherwise have needed exemptions.

    So: this catches a forgetful reader that decides in Python, in this class,
    by naming a delivery state. That is narrower than "a reader added
    tomorrow", which is what this docstring used to claim. The residual is
    real and the human accepted it against model 3; an overclaim here is worse
    than the narrower guarantee honestly described, because the overclaim is
    what gets quoted into a decision.
    """
    gates = _delivery_state_gates(ast.parse(_DATA_ACCESS.read_text(encoding="utf-8")))

    # THE ENUMERATION MUST NOT BE EMPTY, and this is not padding. If the AST
    # shape above ever stops matching anything -- a refactor to a module-level
    # constant, a decorator rename -- `offenders` is empty and the assertion
    # below passes forever while detecting nothing. That is the cannot-fail
    # shape this project has already been bitten by four times in one night.
    assert gates, (
        "the delivery-state enumeration matched NO methods at all; the AST shape it looks for has "
        "stopped describing this file and it is now detecting nothing"
    )
    # And it must still find the gates we know are there, BY NAME, so a method
    # cannot slip out of scope by losing its decorator or rewriting its
    # comparison into a shape the walk does not see. Each of the last three
    # pins one ARM of the w123 widening, so that reverting the widening fails
    # here rather than passing quietly on a matcher that sees less:
    #   retract_report      the `not in (...)` gate (unchanged behaviour)
    #   _read_retracted     equality against a SINGLE delivery state
    #   _read_superseded_by an UNDECORATED helper
    assert {
        "require_release",
        "_release_report",
        "create_amendment",
        "retract_report",
        "_read_retracted",
        "_read_superseded_by",
    } <= set(gates), sorted(gates)

    offenders = sorted(
        name for name, composes in gates.items() if not composes and name not in RETRACTION_COMPOSITION_EXEMPT
    )
    assert not offenders, (
        f"DataAccess method(s) that gate on {sorted(DELIVERY_STATES)} without composing retraction: "
        f"{offenders}. A reader that checks approval and forgets retraction will hand over a report "
        f"the lab has withdrawn, and no database constraint stops it (that is the accepted residual of "
        f"the approved model). Call self.{RETRACTION_READER}(session, report_id) and decide what to do "
        f"with the answer, or add the method to RETRACTION_COMPOSITION_EXEMPT with a ruling behind it."
    )


def test_the_exempt_gates_still_exist():
    """
    An exemption for a method that has been renamed or deleted is a hole
    nobody can see. Pairs with the assertion above: without this, emptying
    the codebase would satisfy it.
    """
    gates = _delivery_state_gates(ast.parse(_DATA_ACCESS.read_text(encoding="utf-8")))
    stale = sorted(RETRACTION_COMPOSITION_EXEMPT - set(gates))
    assert not stale, f"exempted method(s) that no longer gate on the delivery states: {stale}"


# ─── w124: the census the definite article was claiming ───────────────────────
#
# THE DEFECT THIS EXISTS FOR WAS A SENTENCE, not a behaviour. w122 wrote that
# _read_retraction_row is "THE ONE PLACE retraction is read from the database";
# _read_retracted was already reaching the same table with its own SELECT, and
# clinical/retention.py's tombstone cascade with a third. HUMAN RULING (w124):
# fix the claim rather than the code -- the helper is a `_query_one` and
# _read_retracted must see a SECOND row in order to refuse it, so routing the
# reader through the helper would cost a real refusal to buy a true sentence.
#
# THE TELL, in the human's words: THE DEFINITE ARTICLE. "The single read
# point", "the one gate", "the only place" -- each is claiming a census, and a
# census has to be measured. This test is that measurement, kept executable so
# the corrected sentence cannot rot back into the claim it replaced. It reads
# the docstring it is about, in the shape TestHpoTermsDocstringCitations uses.

_RETENTION = pathlib.Path(__file__).resolve().parent.parent / "retention.py"

# A SELECT against the table, in the only form this codebase writes queries:
# an SQL string literal inside a method. UPDATE/INSERT are writes and are not
# counted; the claim under test is about READS.
_RETRACTION_TABLE_READ = re.compile(r"FROM\s+report_retractions")

# Measured, at w124, over clinical/data_access.py and clinical/retention.py.
# Every one of these is named in _read_retraction_row's docstring with what it
# is for. A fourth reader makes this test red, which is the point: the
# docstring now enumerates, and an enumeration nobody re-measures is the
# census claim again with extra steps.
EXPECTED_RETRACTION_TABLE_READERS = {
    "data_access.py::_read_retraction_row",
    "data_access.py::_read_retracted",
    "retention.py::_cascade_tombstone_report_dependents",
    # Added after w124; each composes retraction inline in its own SQL:
    "data_access.py::read_signoff_state",
    "data_access.py::list_patient_genome_analyses",
}

# The four DataAccess methods that ask the retraction question through the
# helper. The w122 docstring listed "the delivery gate, the release recorder,
# the revision reader, get_report" -- and the revision reader (_read_retracted)
# was never one of them, while retract_report's own already-retracted refusal
# was left out. Measured here so a wrong roster cannot be written twice.
EXPECTED_HELPER_CALLERS = {
    "get_report",
    "_release_report",
    "require_release",
    "retract_report",
}


def _functions_with_sql_matching(path, pattern):
    """
    Names of functions in `path` holding an SQL string literal that matches.

    The DOCSTRING IS SKIPPED DELIBERATELY. This file's whole subject is a
    docstring that talks about queries; a prose mention of one is not one, and
    counting it would make the guard agree with whatever the docstring says.
    """
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        for statement in body:
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and pattern.search(inner.value):
                    found.add(node.name)
    return found


def _data_access_methods_calling(attr):
    tree = ast.parse(_DATA_ACCESS.read_text(encoding="utf-8"))
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "DataAccess":
            continue
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name == attr:
                continue
            for inner in ast.walk(method):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == attr:
                    callers.add(method.name)
    return callers


def _retraction_reader_docstring():
    for node in ast.walk(ast.parse(_DATA_ACCESS.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == RETRACTION_READER:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{RETRACTION_READER} is gone from clinical/data_access.py; its docstring cannot be checked")


def test_the_retraction_reader_docstring_names_every_reader_of_the_table():
    """
    The corrected sentence is true of the code, measured rather than recalled.

    Two things, and each fails for a different real reason:
      1. the set of functions that SELECT from report_retractions is exactly
         the five the docstring enumerates -- a fourth reader, or the loss of
         one, is red;
      2. the docstring names each of the other two readers, so the
         enumeration cannot be quietly emptied back to the helper alone.

    NO GREP FOR THE OFFENDING PHRASE, deliberately. The corrected docstring
    QUOTES the false sentence in order to record that it was false, so a check
    for "the one place" would fire on the correction itself. Measuring the
    readers is the check that has teeth anyway: the phrase was only ever wrong
    because the set had three members.

    WHAT IT CANNOT SEE, stated rather than implied: SQL built by concatenation
    or f-string, a read through a view, and any module other than
    data_access.py and retention.py. It is the same AST blind spot the E10
    enumeration above documents, and for the same reason.
    """
    measured = {
        f"data_access.py::{name}" for name in _functions_with_sql_matching(_DATA_ACCESS, _RETRACTION_TABLE_READ)
    }
    measured |= {f"retention.py::{name}" for name in _functions_with_sql_matching(_RETENTION, _RETRACTION_TABLE_READ)}

    assert measured, (
        "no function in data_access.py or retention.py appears to SELECT from report_retractions at all; "
        "the SQL shape this guard looks for has stopped describing the codebase and it is now measuring nothing"
    )
    assert measured == EXPECTED_RETRACTION_TABLE_READERS, (
        f"the readers of report_retractions have changed: {sorted(measured)}. _read_retraction_row's docstring "
        f"enumerates them by name and says what each is for; update the docstring AND this set together, and "
        f"do not replace the enumeration with a count or with 'the one place' -- that sentence was already "
        f"false once (w124)."
    )

    docstring = _retraction_reader_docstring()
    for name in ("_read_retracted", "retention.py", "read_signoff_state", "list_patient_genome_analyses"):
        assert name in docstring, (
            f"{RETRACTION_READER}'s docstring no longer names {name!r}, which reads the same table with its "
            f"own SELECT. A docstring that stops naming the other readers is back to claiming to be the only one."
        )


def test_the_retraction_reader_docstring_names_its_actual_callers():
    """
    The w122 docstring's roster of composers named a method that does not call
    it and omitted one that does. Rosters in prose drift; this one is read.
    """
    callers = _data_access_methods_calling(RETRACTION_READER)
    assert callers == EXPECTED_HELPER_CALLERS, (
        f"the callers of {RETRACTION_READER} have changed: {sorted(callers)}. Its docstring lists them with "
        f"the ruling each one serves; update both."
    )
    docstring = _retraction_reader_docstring()
    missing = sorted(name for name in EXPECTED_HELPER_CALLERS if name not in docstring)
    assert not missing, f"{RETRACTION_READER}'s docstring does not name its caller(s) {missing}"
