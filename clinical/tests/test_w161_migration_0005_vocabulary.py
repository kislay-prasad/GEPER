"""
clinical/tests/test_w161_migration_0005_vocabulary.py
─────────────────────────────────────────────────────
w161 — MIGRATION 0005, THE EXCEPTION-AMENDMENT ACTION VOCABULARY, and the
first DESTRUCTIVE migration this stack has ever carried.

WHAT IS BEING PROVED, AND WHY IT IS NOT THE SAME SHAPE AS 0003 OR 0004. Those
two were ADDITIVE: they created objects, so "did it work" and "did it break
anything" were nearly the same question, and every assertion could be a
presence probe. 0005 DROPS a constraint and re-adds a wider one. Presence is
now the weakest thing that can be asserted: a database whose action CHECK has
been dropped and never restored PASSES every presence probe ever written about
`exception_events`, and accepts the string 'banana' in its action column.

So the four properties below are asserted SEPARATELY, because each fails
differently and three of them are invisible to the other's test:

  1. THE VOCABULARY IS WIDENED — 'amend_resolution' is writable.
  2. NOTHING EXISTING IS REJECTED — and proved with rows planted BEFORE the
     migration runs, not only with writes attempted after it.
  3. THE CONSTRAINT IS STILL A CONSTRAINT — a fifth token is still refused.
     Without this row, (1) is satisfied by dropping the CHECK entirely.
  4. FRESH AND UPGRADED CONVERGE — the same `pg_get_constraintdef`, byte for
     byte, on a database built from `clinical/schema.sql` and on one carried
     up by the migrations.

─── THE WRONG VERSIONS, AND WHICH ROWS CATCH EACH ───

Every one of these was BUILT AND RUN, and the counts were read rather than the
colour (Rule 10). Every row named here takes either no fixture at all or a
scratch database it creates itself, so none of them can SKIP: a skip and a pass
are the same green dot, and a wrong-version experiment that skips is a failed
experiment, not a result.

(A) 0005 DECLARING NO POST-CONDITION — the w160 rule's first real customer.
    Caught by `TestTheW160RuleBitesOnThisVeryFile`, which strips the
    `MIGRATION-ASSERT` line from 0005's own bytes, hands the resulting set to
    `migrate()`, and requires `MigrationNotVerifiable` AND that nothing was
    applied. Paired with the positive twin: the same set with the line put back
    applies and reports ok.
    VACUOUS AGAINST: everything about the DDL. A 0005 containing `SELECT 1`
    would pass it.

(B) THE CONSTRAINT RECREATED WITHOUT THE EXISTING TOKENS.
    Caught by `TestNothingExistingIsRejected::test_rows_written_before_the_
    migration_survive_it` (the migration would FAIL to commit — ADD CONSTRAINT
    validates existing rows) and by
    `test_each_old_verb_is_still_writable_afterwards`. Also caught, without any
    row at all in the table, by the post-condition itself: it requires all four
    tokens in the rendered definition.
    VACUOUS AGAINST: A and D.

(C) `schema.sql` AND THE MIGRATION DISAGREEING.
    Caught by `TestFreshAndUpgradedConverge`, and independently by the drift
    detector this repository already has —
    `test_w126m_schema_equivalence.py::test_schema_sql_and_the_migration_set_
    produce_the_same_catalog` — which diffs `pg_get_constraintdef` across the
    whole schema. Both are kept: the whole-catalogue diff is the mechanism, and
    the row here names THIS constraint so the failure is readable.
    VACUOUS AGAINST: A. Two databases can agree perfectly on a definition that
    declares nothing.

(D) THE CONSTRAINT DROPPED AND NEVER RE-ADDED.
    Caught by `TestTheConstraintIsStillAConstraint`, which attempts a token
    that is in no vocabulary and requires the database to refuse it — the
    positive-twin discipline Rule 4 asks for, since every other row in this
    file would pass against a table with no CHECK at all. Also caught by the
    post-condition's `contype = 'c'` clause and by convergence (C).
    VACUOUS AGAINST: B, whose constraint exists and is merely too narrow.

(E) THE POST-CONDITION SATISFIED BY THE OLD DEFINITION.
    Caught by `TestThePostConditionActuallyDistinguishesTheGenerations`, which
    evaluates 0005's OWN declared post-conditions against a real database
    stopped at generation 4 and requires them to be FALSE, then applies 0005
    and requires them to be TRUE. This is the row the whole wave turns on: a
    post-condition that is true before the migration measures nothing, and
    `to_regclass('exception_events')` — which would satisfy w160's rule — is
    exactly that.
    VACUOUS AGAINST: nothing in the DDL. It is a statement about the
    declaration.

─── ATOMICITY ───

`TestTheDestructiveStepIsAtomic` is the one that could not be written as a
presence probe. It proves there is no window in which the table has no action
CHECK, by making the SECOND statement fail and requiring the FIRST to be undone
with it — the old three-verb constraint still present, unchanged, and the
history row absent. That is the property the human named, and it is proved by
breaking it rather than by asserting that `_apply` looks transactional.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from clinical import migrate
from clinical.models.exception import ExceptionEventAction
from clinical.tests.migration_db import (
    SENTINEL_NOTE,
    SENTINEL_ORG,
    connect,
    plant_sentinel,
    prebuild_consents_index,
    read_sentinel,
    scratch_database,
)
from clinical.tests.test_w126m_schema_equivalence import catalog_of, describe_difference

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

VOCABULARY_VERSION = 5
VOCABULARY_NAME = "exception_amendment_vocabulary"

# The object 0005 is FOR. Written out here rather than read out of the
# migration file: a test that reads its expectations from the file under test
# agrees with that file by construction.
ACTION_CHECK = "exception_events_action_check"

OLD_TOKENS = ("open", "resolve", "reopen")
NEW_TOKEN = "amend_resolution"
ALL_TOKENS = OLD_TOKENS + (NEW_TOKEN,)

# THE DEFINITION AS THE SERVER RENDERS IT, both before and after. MEASURED on
# PostgreSQL 16.15 against a real table, not transcribed from the DDL: an
# inline `CHECK (action IN (...))` and an explicit
# `ADD CONSTRAINT ... CHECK (action IN (...))` normalise to the SAME text, and
# that identity is the whole reason the fresh and upgraded paths can converge.
OLD_DEF = "CHECK ((action = ANY (ARRAY['open'::text, 'resolve'::text, 'reopen'::text])))"
WIDENED_DEF = "CHECK ((action = ANY (ARRAY['open'::text, 'resolve'::text, 'reopen'::text, 'amend_resolution'::text])))"

# A token in no vocabulary, used as the positive twin for wrong-version D.
NOT_A_VERB = "w161_not_a_verb"


# ─── helpers ────────────────────────────────────────────────────────────────


def _apply_sql(connection, sql: str) -> None:
    with connection.cursor() as cur:
        cur.execute(sql)
    connection.commit()


def _build_fresh_from_schema_sql(connection) -> None:
    """The production fresh-database path. It stamps its own history."""
    _apply_sql(connection, SCHEMA_PATH.read_text(encoding="utf-8"))


def _migrations_below_five() -> tuple:
    """The real set, stopped one short of 0005 — DERIVED from `discover()`, so
    a later migration 0006 does not silently join it."""
    return tuple(m for m in migrate.discover() if m.version < VOCABULARY_VERSION)


def _migrations_through_five() -> tuple:
    """
    The real set, stopped AT 0005 -- the missing other half of the helper
    above, and its absence was a latent defect in this file rather than a new
    requirement.

    MEASURED at w164, the first wave to add a migration after this one. Two
    rows below built their "generation 5" side with `migrate.discover()` or a
    bare `migrate.migrate(connection)`, which applies EVERYTHING ON DISK. While
    0005 was the last migration those were the same set, so the rows passed and
    said what they meant. The moment 0006 landed they compared generation 4
    with generation SIX, and reported 0006's column, its type and its four
    INHERITED column privileges as 0005's blast radius -- a scope claim about
    0005 that any later, unrelated migration could falsify.

    The comment on `_migrations_below_five` had already seen the hazard from
    one side ("so a later migration 0006 does not silently join it"); this is
    the other side of the same sentence.
    """
    return tuple(m for m in migrate.discover() if m.version <= VOCABULARY_VERSION)


def _the_migration() -> migrate.Migration:
    by_version = {m.version: m for m in migrate.discover()}
    assert VOCABULARY_VERSION in by_version, "migration 0005 is not on disk"
    return by_version[VOCABULARY_VERSION]


def _on_disk_versions() -> list:
    """Derived from `discover()`, never hard-coded — w146's finding, and the
    shape `test_w151_migration_declaration_guards.py` scans the whole suite
    for."""
    return [m.version for m in migrate.discover()]


def _constraint_def(connection, name: str = ACTION_CHECK):
    """What the SERVER says the constraint is, not what its name suggests."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass('exception_events') AND conname = %s AND contype = 'c'",
            (name,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _tokens_from_the_database(connection) -> set:
    """
    The vocabulary AS THE DATABASE HOLDS IT, parsed out of the rendered
    definition rather than out of any Python constant. This is what makes the
    enum-parity row a measurement: both sides are read from somewhere else.
    """
    definition = _constraint_def(connection)
    assert definition, f"{ACTION_CHECK} is not on exception_events at all"
    return set(part.split("'::text")[0] for part in definition.split("'")[1::2] if part)


def _seed_exception(cur) -> tuple:
    """
    The minimum row chain an `exception_events` row needs, in raw SQL:
    organisation → patient + test → consent → order → exception.

    RAW SQL AND NOT `DataAccess`, and that is a deliberate departure from
    Standing Rule 2a rather than a lapse. Rule 2a governs tests that prove an
    ACCESS RULE -- those must call the method under test, because a raw query
    passes while the method is broken. Nothing here is an access rule: the
    subject is a CHECK CONSTRAINT, the assertion is whether the DATABASE
    accepts a string, and the scaffolding exists only to satisfy the foreign
    keys in between. Going through `DataAccess` would additionally need users,
    three role assignments and a bcrypt login per scratch database, none of
    which the constraint can see.
    """
    cur.execute(
        "INSERT INTO organisations (name, created_at) VALUES (%s, now()) RETURNING org_id",
        (f"w161 {uuid.uuid4()}",),
    )
    org_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO patients (org_id, name, dob, sex, created_at) "
        "VALUES (%s, 'w161 Patient', DATE '1990-01-01', 'U', now()) RETURNING patient_id",
        (org_id,),
    )
    patient_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO tests (org_id, name, assembly, created_at) "
        "VALUES (%s, 'w161 Panel', 'GRCh38', now()) RETURNING test_id",
        (org_id,),
    )
    test_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO consents (patient_id, org_id, scope, recorded_by, recorded_at) "
        "VALUES (%s, %s, 'testing', %s, now()) RETURNING consent_id",
        (patient_id, org_id, uuid.uuid4()),
    )
    consent_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO orders (patient_id, org_id, test_id, required_scope, ordered_by, consent_id) "
        "VALUES (%s, %s, %s, 'testing', %s, %s) RETURNING order_id",
        (patient_id, org_id, test_id, uuid.uuid4(), consent_id),
    )
    order_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO exceptions (org_id, order_id, category, reason_code, status, owner, created_at) "
        "VALUES (%s, %s, 'validation_failure', 'qc_failed', 'open', 'lab_operator', now()) RETURNING id",
        (org_id, order_id),
    )
    return org_id, cur.fetchone()[0]


# The event table's name, held in a constant rather than inlined into the
# statement below. NOT COSMETIC: `test_w149_migration_0004_amendment.py::
# test_5b_nothing_in_the_repository_writes_an_amendment` scans every `.py`
# under `clinical/` and `geper/` for the literal `INSERT INTO exception_events`
# followed within 400 characters by `amends_event_id`, and that guard is
# correct and must not be weakened. This file legitimately writes an amendment
# -- proving one can be written is the point of the wave -- so 0005's commit
# narrows that guard to PRODUCTION modules explicitly, and this constant keeps
# the scan's own marker out of a file that is not production either way.
_EVENTS = "exception_events"


def _write_event(cur, org_id, exception_id, action, *, amends=None) -> None:
    cur.execute(
        f"INSERT INTO {_EVENTS} (org_id, exception_id, actor, timestamp, action, amends_event_id) "  # noqa: S608
        "VALUES (%s, %s, 'w161', now(), %s, %s)",
        (org_id, exception_id, action, amends),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE FILE — structural rows, and they take NO database fixture (Rule 10)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheMigrationIsOnDiskAndNothingFrozenMoved:
    """
    A claim about the FILES needs no database and must not be ABLE to skip.
    Taking a fixture these rows do not use is precisely what would grant them
    that ability.
    """

    def test_the_set_is_contiguous_and_ordered_and_includes_five(self):
        discovered = migrate.discover()
        versions = [m.version for m in discovered]
        assert versions == sorted(versions)
        assert versions == list(range(1, len(versions) + 1)), versions
        assert VOCABULARY_VERSION in versions
        assert {m.version: m.name for m in discovered}[VOCABULARY_VERSION] == VOCABULARY_NAME

    def test_the_four_earlier_migrations_are_byte_identical(self):
        """
        THE GATE, AS A TEST. 0001–0004 are committed and frozen; 0002's, 0003's
        and 0004's checksums are stamped into `clinical/schema.sql` and an edit
        to any of them makes every already-adopted database raise
        MigrationChecksumMismatch.

        CATCHES: a 0005 landed by editing 0004's CHECK instead of adding a
        file — which is the shortcut this wave was explicitly forbidden, and
        which would also make `0004_exception_amendment.sql` contain a DROP.
        """
        by_version = {m.version: m for m in migrate.discover()}
        assert by_version[1].checksum == "00d8c90bb0eaba561d843cde7a45eca5af4d594aca6a3e230938aef27286f559"
        assert by_version[2].checksum == "cc842c3943b7aa1e5786d638eff742f63fd47ce5a5048d7321036234a5290af8"
        assert by_version[3].checksum == "d0e0ae8d149aa20aea64e18e47424f48efb63d0a7ccdd5cbfd9b3f8016202204"
        assert by_version[4].checksum == "a379abc3cf0a81da3af438f4bc092648ab196d1c53e5039cef106a02200743a0"

    def test_schema_sql_stamps_0005_with_the_checksum_of_the_file_on_disk(self):
        """The pair that cannot be half-done: the DDL goes into `schema.sql`
        AND its stamp goes into the INSERT at the foot of that file."""
        migration = _the_migration()
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert f"({VOCABULARY_VERSION}, '{VOCABULARY_NAME}', '{migration.checksum}')" in schema

    def test_0005_is_the_only_migration_that_drops_anything(self):
        """
        THE DESTRUCTIVE VERB IS FENCED TO ONE FILE. Comment lines are stripped
        first, because three of these files DISCUSS `DROP` at length and a
        naive substring scan would be satisfied by the prose rather than by the
        DDL — the substring-credits-a-docstring failure this floor has already
        been bitten by twice.

        CATCHES: a later editor adding a DROP to an additive migration, and the
        version of this wave that widened the CHECK by editing 0004.
        """
        dropping = set()
        for migration in migrate.discover():
            body = "\n".join(line for line in migration.sql.splitlines() if not line.strip().startswith("--")).upper()
            if "DROP " in body:
                dropping.add(migration.version)
        assert dropping == {VOCABULARY_VERSION}, dropping

    def test_0005_drops_exactly_one_constraint_and_adds_exactly_one_back(self):
        """
        The scope of the destruction, on the bytes. One DROP CONSTRAINT, one
        ADD CONSTRAINT, no DROP TABLE, no DROP COLUMN, no RENAME, no DELETE,
        no TRUNCATE, and no NOT VALID — which would create a constraint that
        exists and is not enforced against the rows already there.
        """
        body = "\n".join(
            line for line in _the_migration().sql.splitlines() if not line.strip().startswith("--")
        ).upper()
        assert body.count("DROP CONSTRAINT") == 1, body
        assert body.count("ADD CONSTRAINT") == 1, body
        for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP INDEX", "RENAME ", "DELETE ", "TRUNCATE ", "NOT VALID"):
            assert forbidden not in body, f"0005 contains {forbidden.strip()!r}"
        assert body.count(ACTION_CHECK.upper()) == 2, "the DROP and the ADD must name the same constraint"

    def test_the_post_condition_is_a_constraint_definition_probe_not_a_table_probe(self):
        """
        THE NEW RULE'S SHAPE, NOT JUST ITS LETTER. w160 requires a migration to
        declare something; it cannot require the declaration to be worth
        anything. Here it must be, because the only object 0005 changes already
        existed: `to_regclass('exception_events')` is true in this generation,
        in the amendment generation, in the retraction generation, in the
        baseline generation and in the pre-security generation w143 measured.

        So the declaration is asserted to read the CONSTRAINT DEFINITION out of
        `pg_catalog`. The complementary row — that it is actually FALSE one
        generation back — is `TestThePostConditionActuallyDistinguishesThe
        Generations`, and without that one this row is satisfied by a probe
        that merely LOOKS strong.
        """
        declared = migrate.post_conditions(_the_migration())
        assert declared, "0005 declares no post-condition at all"
        joined = " ".join(invariant.expression for invariant in declared)
        assert "pg_constraint" in joined, joined
        assert "pg_get_constraintdef" in joined, joined
        assert "information_schema" not in joined, joined
        assert ACTION_CHECK in joined, joined
        for token in ALL_TOKENS:
            assert token in joined, f"the post-condition does not mention {token!r}"

    def test_the_python_enum_names_the_same_four_tokens_as_the_column(self):
        """
        COLUMN AND ENUM MUST NOT DRIFT APART, half one: against the DDL text,
        with no database, so this half cannot skip. Half two reads the tokens
        out of a real database and is in `TestTheEnumAndTheColumnAgree`.
        """
        assert {member.value for member in ExceptionEventAction} == set(ALL_TOKENS)
        assert ExceptionEventAction.AMEND_RESOLUTION.value == NEW_TOKEN
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert f"action IN ('open', 'resolve', 'reopen', '{NEW_TOKEN}')" in schema


# ═══════════════════════════════════════════════════════════════════════════
# 2. WRONG-VERSION A — the w160 rule, proved to bite on THIS file
# ═══════════════════════════════════════════════════════════════════════════


def _set_without_the_declaration(root: pathlib.Path) -> pathlib.Path:
    """
    The REAL migration set, byte for byte, with 0005's `MIGRATION-ASSERT` line
    (and only that line) removed. NO FILE IS CREATED UNDER
    `clinical/migrations/`.
    """
    directory = root / "migrations"
    directory.mkdir(parents=True)
    for path in sorted(migrate.MIGRATIONS_DIR.iterdir()):
        if path.is_file():
            (directory / path.name).write_bytes(path.read_bytes())
    target = directory / _the_migration().path.name
    text = target.read_text(encoding="utf-8")
    stripped = "\n".join(line for line in text.splitlines() if "MIGRATION-ASSERT" not in line)
    assert stripped != text, "0005 carries no MIGRATION-ASSERT line to strip"
    target.write_text(stripped + "\n", encoding="utf-8")
    return directory


class TestTheW160RuleBitesOnThisVeryFile:
    def test_the_real_directory_is_never_written_to(self, tmp_path):
        before = {p.name: p.read_bytes() for p in migrate.MIGRATIONS_DIR.iterdir() if p.is_file()}
        _set_without_the_declaration(tmp_path)
        after = {p.name: p.read_bytes() for p in migrate.MIGRATIONS_DIR.iterdir() if p.is_file()}
        assert before == after

    def test_the_stripped_set_differs_from_the_real_one_in_exactly_one_file(self, tmp_path):
        """
        NON-VACUITY FOR THE EXPERIMENT ITSELF. A helper that produced an
        identical copy would make the refusal row below pass or fail for a
        reason having nothing to do with 0005.
        """
        stripped = migrate.discover(_set_without_the_declaration(tmp_path))
        real = {m.version: m for m in migrate.discover()}
        differing = [m.version for m in stripped if real[m.version].checksum != m.checksum]
        assert differing == [VOCABULARY_VERSION], differing
        assert not migrate.post_conditions({m.version: m for m in stripped}[VOCABULARY_VERSION])

    def test_migrate_refuses_the_undeclared_0005_and_applies_nothing(self, tmp_path):
        """
        WRONG-VERSION A. The refusal happens before the advisory lock and
        before any DDL, so the database is left EMPTY rather than half-upgraded
        — which is asserted, because "it raised" and "it changed nothing" are
        two different claims.
        """
        silent = migrate.discover(_set_without_the_declaration(tmp_path))
        with scratch_database("w161_undeclared") as dsn, connect(dsn) as connection:
            with pytest.raises(migrate.MigrationNotVerifiable) as raised:
                migrate.migrate(connection, silent)
            assert f"{VOCABULARY_VERSION:04d}_{VOCABULARY_NAME}" in str(raised.value)
            assert "Nothing was applied" in str(raised.value)
            connection.rollback()
            assert migrate.schema_is_present(connection) is False
            assert migrate.history_table_exists(connection) is False

    def test_the_positive_twin_the_same_set_with_the_line_applies(self, tmp_path):
        """
        THE PAIRING RULE 4 REQUIRES. Without it, a `migrate()` that refused
        EVERYTHING would pass the row above. Same tmp_path mechanism, same
        scratch database, one line different.
        """
        directory = _set_without_the_declaration(tmp_path)
        target = directory / _the_migration().path.name
        target.write_text(_the_migration().path.read_text(encoding="utf-8"), encoding="utf-8")
        restored = migrate.discover(directory)
        with scratch_database("w161_declared") as dsn, connect(dsn) as connection:
            applied = migrate.migrate(connection, restored)
            assert VOCABULARY_VERSION in applied
            assert migrate.inspect_state(connection, restored).ok is True
            assert _constraint_def(connection) == WIDENED_DEF


# ═══════════════════════════════════════════════════════════════════════════
# 3. WRONG-VERSION E — the post-condition must DISTINGUISH the generations
# ═══════════════════════════════════════════════════════════════════════════


class TestThePostConditionActuallyDistinguishesTheGenerations:
    def test_it_is_false_at_generation_four_and_true_at_five(self):
        """
        THE ROW THE WAVE TURNS ON. A post-condition that is already true before
        the migration runs measures nothing: it can never fail, so readiness
        re-evaluating it on every request is a cost with no signal, and the
        history row's claim goes on being unchecked. `to_regclass('exception_
        events')` — which satisfies w160's rule perfectly — is exactly that
        probe, and it would pass at BOTH generations.

        So the database is stopped at 0004, 0005's OWN declared expressions are
        evaluated against it, and every one of them must be FALSE. Then 0005 is
        applied to the same database and every one must be TRUE.

        CATCHES: wrong-version E, and any future weakening of the declaration
        to something a pre-migration database already satisfies.
        """
        below = _migrations_below_five()
        declared = migrate.post_conditions(_the_migration())
        assert declared

        with scratch_database("w161_distinguish") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, below)
            assert _constraint_def(connection) == OLD_DEF

            before = [migrate._evaluate(connection, i.expression) for i in declared]
            assert before == [False] * len(declared), before

            migrate.migrate(connection, migrate.discover())

            after = [migrate._evaluate(connection, i.expression) for i in declared]
            assert after == [True] * len(declared), after
            assert migrate._failed_assertions(connection, _the_migration()) == ()

    def test_readiness_reports_drift_when_the_widened_constraint_is_vandalised(self):
        """
        THE POST-CONDITION IS RE-EVALUATED, NOT JUST EVALUATED ONCE. A history
        row is a CLAIM; `inspect_state` is what turns it into a measurement on
        every `/readyz`. So: a correct database at 0005, then the constraint is
        put back to its old three-verb form behind the runner's back, and the
        verdict must stop being ok and must NAME the drift.

        CATCHES: a post-condition wired only into the apply path, which would
        be true forever afterwards regardless of what happened to the database.
        """
        with scratch_database("w161_vandal") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            assert migrate.inspect_state(connection).ok is True

            with connection.cursor() as cur:
                cur.execute(f"ALTER TABLE exception_events DROP CONSTRAINT {ACTION_CHECK}")
                cur.execute(
                    f"ALTER TABLE exception_events ADD CONSTRAINT {ACTION_CHECK} "
                    "CHECK (action IN ('open', 'resolve', 'reopen'))"
                )
            connection.commit()

            state = migrate.inspect_state(connection)
            assert state.ok is False
            assert state.reason == "schema_drift", state.reason
            assert ACTION_CHECK in (state.detail or ""), state.detail


# ═══════════════════════════════════════════════════════════════════════════
# 4. ATOMICITY — there is no window in which the table has no action CHECK
# ═══════════════════════════════════════════════════════════════════════════


class TestTheDestructiveStepIsAtomic:
    def test_a_failure_after_the_drop_undoes_the_drop(self):
        """
        THE HUMAN'S FIRST REQUIREMENT, PROVED BY BREAKING IT. A doctored 0005 —
        the real DROP, then a statement that fails — is applied to a database at
        generation 4. Afterwards the OLD three-verb constraint must still be
        there, unchanged, and the history row must be absent.

        This is not a claim about `_apply` looking transactional; it is the
        measurement that PostgreSQL's transactional DDL actually covers a DROP
        CONSTRAINT in this runner's hands. A version of `_apply` that committed
        between statements would leave the table with NO action CHECK and this
        row would fail.

        THE CONTROL IS THE ROW BELOW: the same mechanism with a statement that
        SUCCEEDS does land the widened constraint, so "nothing changed" here is
        a consequence of the failure and not of the harness.
        """
        real = _the_migration()
        drop_only = migrate.Migration(
            version=real.version,
            name=real.name,
            path=real.path,
            sql=(f"ALTER TABLE exception_events DROP CONSTRAINT {ACTION_CHECK};\nSELECT w161_no_such_function();\n"),
            checksum=real.checksum,
            assertions=real.assertions,
        )

        with scratch_database("w161_atomic") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _migrations_below_five())
            assert _constraint_def(connection) == OLD_DEF

            with pytest.raises(Exception):
                migrate._apply(connection, drop_only)
            connection.rollback()

            assert _constraint_def(connection) == OLD_DEF, "the DROP was not undone"
            assert VOCABULARY_VERSION not in [a.version for a in migrate.read_history(connection)]

            # AND THE TABLE IS STILL GUARDED — the point of the property. A
            # rolled-back DROP that left an unguarded column would satisfy the
            # definition assertion above only if the definition were read from
            # somewhere other than the catalogue; this writes to it.
            with connection.cursor() as cur:
                org_id, exception_id = _seed_exception(cur)
                with pytest.raises(Exception):
                    _write_event(cur, org_id, exception_id, NOT_A_VERB)
            connection.rollback()

    def test_the_control_the_real_0005_through_the_same_path_does_land(self):
        """THE POSITIVE TWIN. Same database shape, same `_apply`, the real
        file — so the row above measures the failure and not the harness."""
        with scratch_database("w161_atomic_ok") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _migrations_below_five())
            migrate._apply(connection, _the_migration())
            assert _constraint_def(connection) == WIDENED_DEF
            assert VOCABULARY_VERSION in [a.version for a in migrate.read_history(connection)]

    def test_the_file_is_one_statement_batch_and_the_runner_commits_once(self):
        """
        WHERE THE ATOMICITY COMES FROM, said as an assertion rather than as a
        comment: `_apply` sends `migration.sql` in ONE `cur.execute` and calls
        `commit()` exactly once. Read from the AST, so the prose in that
        function's long docstring cannot satisfy it.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(migrate._apply))
        executes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
        ]
        commits = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "commit"
        ]
        assert len(commits) == 1, ast.dump(tree)
        assert any(
            isinstance(call.args[0], ast.Attribute) and call.args[0].attr == "sql" for call in executes if call.args
        ), "the migration body is not sent as one execute of migration.sql"


# ═══════════════════════════════════════════════════════════════════════════
# 5. WRONG-VERSION B — nothing existing is rejected, proved on rows that were
#    already there
# ═══════════════════════════════════════════════════════════════════════════


class TestNothingExistingIsRejected:
    def test_rows_written_before_the_migration_survive_it(self):
        """
        THE HUMAN'S SECOND REQUIREMENT, and the emphasis is his: proved with
        rows present in the table BEFORE the migration runs, not only with
        writes attempted after it. A superset demonstrated on an empty table is
        a superset demonstrated against nothing.

        The mechanism that makes this more than a hope: ADD CONSTRAINT without
        NOT VALID makes PostgreSQL validate every existing row inside the
        migration's own transaction. Wrong-version B — the constraint recreated
        without the old tokens — therefore does not silently reject old rows
        later; it FAILS TO COMMIT, and the sentinel below proves the database
        was not quietly rebuilt underneath the assertion instead.
        """
        with scratch_database("w161_existing") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _migrations_below_five())
            plant_sentinel(connection)

            with connection.cursor() as cur:
                org_id, exception_id = _seed_exception(cur)
                for token in OLD_TOKENS:
                    _write_event(cur, org_id, exception_id, token)
            connection.commit()

            # This database holds a consent, so migration 0011 adopts an index the OPERATOR builds first.
            prebuild_consents_index(dsn)
            migrate.migrate(connection, migrate.discover())

            notes, organisations = read_sentinel(connection)
            assert SENTINEL_NOTE in notes
            assert SENTINEL_ORG in organisations

            with connection.cursor() as cur:
                cur.execute(
                    "SELECT action FROM exception_events WHERE exception_id = %s ORDER BY action",
                    (exception_id,),
                )
                assert sorted(r[0] for r in cur.fetchall()) == sorted(OLD_TOKENS)
            connection.rollback()

            assert _constraint_def(connection) == WIDENED_DEF

    def test_each_old_verb_is_still_writable_afterwards(self):
        """Surviving is not the same as still being writable: a constraint can
        be valid against the past and refuse the future."""
        with scratch_database("w161_stillwrites") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            with connection.cursor() as cur:
                org_id, exception_id = _seed_exception(cur)
                for token in OLD_TOKENS:
                    _write_event(cur, org_id, exception_id, token)
                cur.execute("SELECT count(*) FROM exception_events WHERE exception_id = %s", (exception_id,))
                assert cur.fetchone()[0] == len(OLD_TOKENS)
            connection.rollback()


# ═══════════════════════════════════════════════════════════════════════════
# 6. THE VOCABULARY IS WIDENED — and WRONG-VERSION D, the constraint dropped
#    and never re-added
# ═══════════════════════════════════════════════════════════════════════════


class TestTheVocabularyIsWidenedAndStillAVocabulary:
    def test_an_amendment_action_can_now_be_written_at_all(self):
        """
        THE POINT OF THE WAVE, at the database rather than in the catalogue: an
        event carrying `amend_resolution` AND the `amends_event_id` migration
        0004 landed is now a row this database will accept. Before 0005 no
        value denoted an amendment, so 0004's relationship was unreachable by
        construction.
        """
        with scratch_database("w161_amend") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            with connection.cursor() as cur:
                org_id, exception_id = _seed_exception(cur)
                _write_event(cur, org_id, exception_id, "resolve")
                cur.execute("SELECT id FROM exception_events WHERE exception_id = %s", (exception_id,))
                target = cur.fetchone()[0]
                _write_event(cur, org_id, exception_id, NEW_TOKEN, amends=target)
                cur.execute("SELECT action, amends_event_id FROM exception_events WHERE amends_event_id IS NOT NULL")
                assert cur.fetchall() == [(NEW_TOKEN, target)]
            connection.rollback()

    def test_the_same_write_is_REFUSED_one_generation_back(self):
        """
        NON-VACUITY FOR THE ROW ABOVE, and the measurement that 0005 is what
        made the difference. The identical INSERT against a database stopped at
        0004 must be refused by the CHECK.
        """
        with scratch_database("w161_amend_before") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _migrations_below_five())
            with connection.cursor() as cur:
                org_id, exception_id = _seed_exception(cur)
                with pytest.raises(Exception) as raised:
                    _write_event(cur, org_id, exception_id, NEW_TOKEN)
                assert ACTION_CHECK in str(raised.value), str(raised.value)
            connection.rollback()

    def test_a_token_in_no_vocabulary_is_still_refused(self):
        """
        WRONG-VERSION D, AND IT IS THE ROW WITHOUT WHICH THIS WHOLE FILE IS
        SATISFIED BY A DROPPED CONSTRAINT. Every other behavioural row here
        asserts that something IS accepted, and a table with no CHECK at all
        accepts everything. The CHECK must still be a CHECK.
        """
        with scratch_database("w161_notaverb") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            with connection.cursor() as cur:
                org_id, exception_id = _seed_exception(cur)
                with pytest.raises(Exception) as raised:
                    _write_event(cur, org_id, exception_id, NOT_A_VERB)
                assert ACTION_CHECK in str(raised.value), str(raised.value)
            connection.rollback()


class TestTheEnumAndTheColumnAgree:
    def test_the_enum_is_exactly_the_database_s_vocabulary(self):
        """
        COLUMN AND ENUM MUST NOT DRIFT APART, half two — and both sides are
        READ rather than asserted against one another's source. The tokens come
        out of a real database's rendered constraint definition; the members
        come out of the module. A wave that widens one and forgets the other
        fails here on the day it lands rather than on the day something tries
        to write the missing verb.
        """
        with scratch_database("w161_enum") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            assert _tokens_from_the_database(connection) == {m.value for m in ExceptionEventAction}

    def test_the_parity_row_can_actually_fail(self):
        """
        NON-VACUITY: `_tokens_from_the_database` returning an empty set would
        make the row above pass only against an empty enum, but a parser that
        returned the enum's own values by accident would make it pass always.
        So the tokens are checked to be the four expected strings, read from
        the database and compared to a literal nothing in this file derives
        from the enum.
        """
        with scratch_database("w161_enum_nv") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            assert _tokens_from_the_database(connection) == set(ALL_TOKENS)


# ═══════════════════════════════════════════════════════════════════════════
# 7. WRONG-VERSION C — fresh and upgraded must be the same database
# ═══════════════════════════════════════════════════════════════════════════


class TestFreshAndUpgradedConverge:
    def test_the_action_check_is_byte_identical_on_both_paths(self):
        """
        THE SINGLE MOST VALUABLE ROW IN THIS WAVE, named directly rather than
        left to the whole-catalogue diff. `clinical/schema.sql` writes this
        CHECK INLINE and unnamed; 0005 re-adds it EXPLICITLY and named. Those
        are two different spellings, and `pg_get_constraintdef` is what proves
        the server normalises them to one definition — including the ORDER of
        the four tokens, which a differently-ordered ADD CONSTRAINT would
        change.

        CATCHES: wrong-version C in its most likely real form — 0005 and
        `schema.sql` widened with the new token in different positions, which
        no presence probe anywhere can see.
        """
        with scratch_database("w161_fresh") as dsn_x, scratch_database("w161_upgraded") as dsn_y:
            with connect(dsn_x) as x, connect(dsn_y) as y:
                _build_fresh_from_schema_sql(x)
                migrate.migrate(y)

                fresh = _constraint_def(x)
                upgraded = _constraint_def(y)

            assert fresh == upgraded, f"{fresh!r} != {upgraded!r}"
            assert fresh == WIDENED_DEF, fresh

    def test_the_whole_catalogue_still_agrees_and_so_does_the_history(self):
        """
        The drift detector this repository already has, re-run for this
        generation. Kept alongside the named row above because the two fail
        differently: this one catches anything 0005 changed that `schema.sql`
        did not, including objects nobody thought to name.
        """
        with scratch_database("w161_cat_x") as dsn_x, scratch_database("w161_cat_y") as dsn_y:
            with connect(dsn_x) as x, connect(dsn_y) as y:
                _build_fresh_from_schema_sql(x)
                migrate.migrate(y)
                left, right = catalog_of(x), catalog_of(y)

                def history(connection):
                    with connection.cursor() as cur:
                        cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
                        return tuple(cur.fetchall())

                assert history(x) == history(y)

            difference = describe_difference("schema.sql", left, "migrations", right)
            assert not difference, difference

    def test_a_real_historical_database_is_carried_up_rather_than_rebuilt(self):
        """
        SENTINEL DISCIPLINE. The convergence rows above build both sides from
        empty, so neither proves an EXISTING database is upgraded rather than
        replaced. Here a database is taken to generation 4, given markers a
        rebuild cannot carry across, then carried to 5 — and both the markers
        and the new definition are asserted.
        """
        with scratch_database("w161_historical") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _migrations_below_five())
            plant_sentinel(connection)
            assert _constraint_def(connection) == OLD_DEF

            # THROUGH five, not everything on disk. `assert applied ==
            # (VOCABULARY_VERSION,)` is the tell: this row is about the step
            # from 4 to 5, and `migrate.discover()` would carry it to whatever
            # the newest migration is. See `_migrations_through_five`.
            applied = migrate.migrate(connection, _migrations_through_five())
            assert applied == (VOCABULARY_VERSION,), applied

            notes, organisations = read_sentinel(connection)
            assert SENTINEL_NOTE in notes
            assert SENTINEL_ORG in organisations
            assert _constraint_def(connection) == WIDENED_DEF
            # Against the SAME set that was applied. A bare `inspect_state`
            # reads every migration on disk and would correctly report 0006 as
            # pending on a database this row deliberately stopped at 0005.
            assert migrate.inspect_state(connection, _migrations_through_five()).ok is True


# ═══════════════════════════════════════════════════════════════════════════
# 8. THE SCOPE BOUNDARY — what 0005 did NOT bring
# ═══════════════════════════════════════════════════════════════════════════


class TestTheScopeBoundaryHolds:
    def test_no_production_module_uses_the_new_enum_member(self):
        """
        THE VERB IS NOT HERE, AND THIS IS THE HALF 0004's ROW 5b CANNOT SEE.
        That row scans `INSERT INTO exception_events` statements for
        `amends_event_id` and so would miss a verb that reached the new
        vocabulary through the enum — `ExceptionEventAction.AMEND_RESOLUTION` —
        without naming the column. Both halves are needed and neither is a
        restatement of the other.

        DECLARED AND UNUSED IS THE INTENDED STATE. The member exists so the
        enum does not drift from the column; nothing produces it. When the
        amendment verb arrives this row goes red, and that is the correct
        signal rather than a nuisance.

        NON-VACUITY, Rule 4: an absence assertion is satisfied by a scan that
        read nothing, so the same walk must also find a member that IS used.
        """
        root = pathlib.Path(__file__).resolve().parent.parent.parent
        scanned = 0
        users_of_new = []
        users_of_old = []
        for base in ("clinical", "geper"):
            for path in sorted((root / base).rglob("*.py")):
                if "tests" in path.parts:
                    continue
                scanned += 1
                text = path.read_text(encoding="utf-8", errors="replace")
                if f"{ExceptionEventAction.__name__}.AMEND_RESOLUTION" in text:
                    users_of_new.append(str(path.relative_to(root)))
                if f"{ExceptionEventAction.__name__}.REOPEN" in text:
                    users_of_old.append(str(path.relative_to(root)))
        assert scanned > 50, scanned
        assert users_of_old, "the walk never reached a production user of this enum at all"
        assert users_of_new == [], users_of_new

    def test_the_pairing_check_is_deliberately_absent(self):
        """
        THE CARD, AS A TEST. The held branch carries
        `exception_events_amendment_names_its_target CHECK ((action =
        'amend_resolution') = (amends_event_id IS NOT NULL))`, and
        `0004_exception_amendment.sql`'s header says it "belongs with the
        vocabulary" — so its absence here is a DIVERGENCE from this branch's own
        earlier expectation and is pinned rather than left to be discovered.

        It is out of scope because it is a SEMANTIC rule about what an
        amendment must carry, not a vocabulary, and settling it means ruling on
        the amendment verb — which this wave was explicitly not authorised to
        do. Its failure is the correct signal for the verb's wave.
        """
        with scratch_database("w161_pairing") as dsn, connect(dsn) as connection:
            migrate.migrate(connection)
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = to_regclass('exception_events') AND contype = 'c' "
                    "ORDER BY conname"
                )
                names = [r[0] for r in cur.fetchall()]
            assert names == [ACTION_CHECK], names

    def test_0005_changed_no_object_but_the_action_check(self):
        """
        THE BLAST RADIUS, MEASURED rather than reasoned: two databases, one at
        generation 4 and one at 5, and the ONLY catalogue difference between
        them is this constraint's definition.

        CATCHES: a 0005 that also dropped an index, moved a grant, or altered a
        column while nobody was reading the diff.
        """
        with scratch_database("w161_before") as dsn_before, scratch_database("w161_after") as dsn_after:
            with connect(dsn_before) as before, connect(dsn_after) as after:
                migrate.migrate(before, _migrations_below_five())
                # THROUGH five, not everything on disk -- see
                # `_migrations_through_five`. A bare `migrate(after)` makes
                # this row's "generation 5" whatever the newest migration
                # happens to be, so any later wave's objects would be reported
                # here as 0005's blast radius.
                migrate.migrate(after, _migrations_through_five())
                left, right = catalog_of(before), catalog_of(after)

        difference = describe_difference("generation 4", left, "generation 5", right)
        assert ACTION_CHECK in difference
        for section in (
            "tables",
            "indexes",
            "table_privileges",
            "column_privileges",
            "functions",
            "triggers",
            "sequences",
        ):
            assert f"── {section} ──" not in difference, difference
        # `constraints` is the one section that may differ, and `columns` must
        # not: a widened CHECK changes no column's type, default or nullability.
        assert "── columns ──" not in difference, difference
        offending = [line for line in difference.splitlines() if line.strip().startswith("only in")]
        assert len(offending) == 2, difference
        assert all(ACTION_CHECK in line for line in offending), difference
