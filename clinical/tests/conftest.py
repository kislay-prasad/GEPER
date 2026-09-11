"""
clinical/tests/conftest.py
───────────────────────────
The fail-not-skip guard.

THE DEFECT THIS EXISTS FOR: twenty-two tests that skip and twenty-two that
pass produce the same exit code, and CI reports both as a green job. A suite
that silently declines to run is worse than no suite, because the gap stops
being visible -- the dashboard says covered.

So: when CLINICAL_REQUIRE_DB is set (CI sets it), every skip in this package
becomes a failure, and a missing DSN fails at collection before any test
runs. Locally the variable is unset and skipping still works, because a
developer without Postgres should be able to run TestStructuralIsolation
without the suite exploding -- there, a skip really is an honest gap and it
is reported as one.

Deliberately broader than "skips caused by the database": under
CLINICAL_REQUIRE_DB ANY skip fails the run. A skip introduced later for some
other reason would be an equally silent hole in the coverage this job exists
to guarantee, and having to add the exception explicitly is the point.

─── and the reachability gate, added 2026-09-10 ───

All of the above turns on the DSN being SET. Nothing here ever checked that
the database was REACHABLE, and that one word is a second defect with the
opposite shape:

    DSN unset,  database down -> 533 of 580 tests SKIP, exit 0.
                                 A green run of 8% of the suite.
    DSN set,    database down -> 533 ERRORS, one per test, each
                                 "psycopg.errors.ConnectionTimeout:
                                 connection timeout expired".

The first case is what the fail-not-skip guard above already handles. The
second is what _clinical_database_is_reachable handles, and it needs the
gate rather than the guard, because an ERROR is not a skip and no amount of
skip-to-failure conversion touches it.

MEASURED, at 1e70f352 (do not soften these numbers; they are why the fixture
is worth its lines): the error names no host, no port, no database and no
container, so ~500 copies of it arrive immediately after somebody's own edit
and the obvious inference -- "I broke something" -- is the wrong one. Each
copy costs the fixtures' connect_timeout of 10s, or 20s when the DSN uses the
hostname `localhost` rather than 127.0.0.1, because psycopg tries both
address families and each burns the full timeout. `localhost` is what this
repository's own documentation and CI both use, so the DOCUMENTED
configuration is the slow one: roughly three hours of identical, useless
errors.

─── and the disposable-database guard, added 2026-09-12 ───

Every database fixture here runs DROP SCHEMA public CASCADE against the
DSN. A set DSN therefore also needs CLINICAL_TEST_DB_DISPOSABLE=1, checked
before the reachability probe opens anything -- see
clinical/tests/db_guard.py for why it is an opt-in and not a blocklist.
"""

from __future__ import annotations

import os

import pytest

REQUIRE_DB_ENV = "CLINICAL_REQUIRE_DB"
DSN_ENV = "CLINICAL_TEST_DSN"

# Short ON PURPOSE, and deliberately unlike the fixtures' 10s. This probe asks
# only "is anything listening and willing to talk Postgres", which a healthy
# server answers in milliseconds. The fixtures' 10s is a different question --
# "can a real query get through a possibly-loaded database" -- and it is
# LOAD-BEARING: see the comment at test_identity.py:100, where it was added to
# stop a run blocking past two minutes. DO NOT reach for those 20 values to
# make a down database fail faster. That is what this fixture is for.
PROBE_TIMEOUT_SECONDS = 3

# The hint is printed only for this exact address. Naming a container that a
# machine never had would be a NEW wrong inference, which is the whole defect
# this fixture exists to remove.
SHARED_CONTAINER = "kelly-clinical-test"
SHARED_CONTAINER_PORT = 5433
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_db() -> bool:
    return os.getenv(REQUIRE_DB_ENV, "").strip() not in ("", "0", "false", "False")


def pytest_configure(config: pytest.Config) -> None:
    """
    Fail at collection, not at test time, when the job demands a database and
    has not been given one. UsageError exits pytest with code 4 before a
    single test runs, so a misconfigured CI job cannot report a green
    "0 failed" for a suite it never executed.
    """
    if not _require_db():
        return
    if not os.getenv(DSN_ENV):
        raise pytest.UsageError(
            f"{REQUIRE_DB_ENV} is set but {DSN_ENV} is not. This job exists to run the "
            "identity suite against a real PostgreSQL; refusing to run a suite that "
            "would skip and report success."
        )


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    """
    Convert every skip into a failure while CLINICAL_REQUIRE_DB is set.

    A hookwrapper on the report rather than a collection-time check on
    purpose: it catches skips from every source -- the requires_db marker,
    `pytest.importorskip` for psycopg, a `pytest.skip()` inside a fixture,
    an xfail that turned into a skip. A collection-time check only sees the
    markers, and the ones that bite are the runtime ones.
    """
    outcome = yield
    if not _require_db():
        return
    report = outcome.get_result()
    if report.skipped:
        report.outcome = "failed"
        reason = getattr(report, "longrepr", None)
        report.longrepr = (
            f"SKIPPED WITH {REQUIRE_DB_ENV} SET -- treated as a failure.\n"
            f"This job must actually execute every test; a skip here is a silent "
            f"coverage hole, not an honest gap.\nOriginal skip reason: {reason}"
        )


def _address_only(dsn: str) -> tuple[str, str, str]:
    """
    host, port, dbname -- AND NOTHING ELSE, EVER.

    The DSN carries a password. A pytest banner goes straight into CI logs and
    into pasted bug reports, so the raw string must never reach one. This
    returns three fields by name rather than redacting the original, because
    redaction is a blacklist and a blacklist is one new DSN parameter away from
    leaking.
    """
    try:
        from psycopg.conninfo import conninfo_to_dict

        parsed = conninfo_to_dict(dsn)
        host = str(parsed.get("host") or "(default)")
        port = str(parsed.get("port") or 5432)
        dbname = str(parsed.get("dbname") or "(default)")
        return host, port, dbname
    except Exception:
        # An unparseable DSN is still a real failure worth reporting; we just
        # cannot name the address. Say that, rather than printing the string.
        return "(unparseable DSN)", "(unknown)", "(unknown)"


def _unreachable_message(host: str, port: str, dbname: str, exc: BaseException) -> str:
    """
    Build the banner. A PURE FUNCTION on purpose, and this is not tidiness:
    the container hint fires only for a local 5433, which is precisely the
    address of a container this code is forbidden to connect to. Extracting
    the message is the only way to DEMONSTRATE that branch rather than assert
    it -- and an unexercised branch in an error path is where wrong advice
    lives, because nothing ever reads it until the day it is wrong.
    """
    lines = [
        "",
        f"{DSN_ENV} is set, but nothing is answering at host={host} port={port} dbname={dbname}.",
        f"  ({type(exc).__name__}: {exc})",
        "",
        "This is almost certainly the database being DOWN, not a fault in your changes.",
        "Refusing to run: without this check every database test in this package errors",
        "separately, each waiting out its own 10s connection timeout (20s when the DSN",
        "uses the hostname 'localhost'), and none of those errors names an address.",
        "",
    ]
    if host in LOCAL_HOSTS and port == str(SHARED_CONTAINER_PORT):
        lines += [
            "That address is where this floor's shared container listens. To check and start it:",
            f"    docker ps -a --filter name={SHARED_CONTAINER}",
            f"    docker start {SHARED_CONTAINER}",
            "A container's RUNNING STATE does not survive a reboot, and nothing in the",
            "repository records it. Starting it is safe; do not stop, remove or clean it.",
            "BUT THIS SUITE DROPS THE SCHEMA OF WHATEVER IT RUNS AGAINST: if that container",
            "is shared, running here erases it. Use a disposable container of your own",
            "(see clinical/tests/db_guard.py).",
            "",
        ]
    else:
        lines += [
            f"Start whatever provides that address, or unset {DSN_ENV} to run the",
            "offline tests only (they will then report as skips, which is honest).",
            "",
        ]
    return "\n".join(lines)


@pytest.fixture(scope="session", autouse=True)
def _clinical_database_is_reachable() -> None:
    """
    Fail once, in three seconds, instead of ~500 times over three hours.

    WHAT THIS IS: the local equivalent of the health gate CI already has. The
    clinical-tests job in .github/workflows/pytest.yml declares its Postgres as
    a service with `--health-cmd pg_isready`, so it cannot run a single test
    against a database that is not ready. The dependency is fully declared in
    exactly one place -- and that place is the one a human never runs. This
    gives the local suite the same property.

    WHY IT EXITS RATHER THAN SKIPS: setting CLINICAL_TEST_DSN is an explicit
    request for database coverage. Skipping in response would recreate the very
    defect the fail-not-skip guard above exists to prevent, and under
    CLINICAL_REQUIRE_DB it would be converted back into a failure anyway.

    WHAT IT COSTS, STATED SO NOBODY IS SURPRISED: pytest.exit stops the whole
    session, including the ~47 tests in this package that need no database at
    all. That is deliberate. The alternative is a partial run that reports
    success, which is this package's founding complaint. To run the offline
    tests deliberately, UNSET the DSN -- then the skips are honest and are
    reported as skips.

    WHAT IT DOES NOT DO: it does not start anything. A suite that can start
    shared infrastructure is a suite that can later be given permission to
    restart it, and this container is shared with other people's work. It would
    also hide the dependency again, which is the opposite of the point.
    """
    dsn = os.getenv(DSN_ENV)
    if not dsn:
        return  # the skip path above is correct and already honest

    # FIRST, before any connection: every database fixture in this package
    # drops the schema of the database the DSN names. A DSN alone is not
    # consent to that -- see clinical/tests/db_guard.py.
    from clinical.tests.db_guard import exit_unless_disposable

    exit_unless_disposable(dsn)

    try:
        import psycopg
    except ImportError:
        # Not our failure to report. The individual fixtures already handle a
        # missing driver, and swallowing it here would hide it.
        return

    try:
        with psycopg.connect(dsn, connect_timeout=PROBE_TIMEOUT_SECONDS):
            return
    except Exception as exc:
        host, port, dbname = _address_only(dsn)
        pytest.exit(_unreachable_message(host, port, dbname, exc), returncode=4)
