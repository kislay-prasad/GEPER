"""
clinical/workflow_endpoints.py
──────────────────────────────
w157 (WAVE 7) — THE LABORATORY BENCH SURFACE. Two routes, two stages of the
operational chain, and nothing else.

WHY THIS FILE EXISTS, STATED AS THE MEASUREMENT AND NOT AS A STORY.
Wave 7's subject is the chain `Order -> Sample -> Processing -> QC ->
Interpretation -> Review -> Report`. Measured at `w126m-migrations` tip
`764d646` by an ALIAS-RESOLVING `ast.Call` census over every non-test module
in the repository — never grep, which credits a docstring as a caller, and
never a bare-name scan, which misses `from review.signoff import approve as
_approve` — the production CALL-SITE count of each stage verb was:

    create_order            0        create_interpretation   0
    place_order             0        create_report           0
    cancel_order            0        create_reanalysis       0
    receive_sample          0        submit_for_review       1  (review CLI)
    record_qc               0        approve_report          1  (review CLI)
    create_sequencing_run   0        _release_report         0
    create_vcf              0        require_release         0
    record_pipeline_result  1        create_amendment        0
                               (submission worker)

and the served route surface — WALKED, not counted from decorators and not
read naively off `app.routes` (Standing Rule 9 as corrected: `include_router`
leaves an opaque `_IncludedRouter` wrapper on fastapi 0.141.1 / starlette
1.6.0) — contained twenty-one routes and NOT ONE of them touched any stage of
the chain except `POST /admin/reports/{report_id}/retraction`.

So `receive_sample` and `record_qc` were IMPLEMENTED, GATED, STATE-CHECKING,
AUDITED AND TESTED AT THE DOMAIN LAYER, and unreachable. The missing link is
the outermost one — the same single missing link `w150-reachability-audit.md`
found in thirteen of fourteen capabilities. This module is that link for the
Sample and QC stages, and for those two only.

WHAT IS AND IS NOT IN IT.
Both handlers are TRANSLATIONS — HTTP in, ONE existing domain method, HTTP
out. No business logic, no second permission system, no second session type,
no second refusal vocabulary, no SQL, and no state machine. The order-state
rules (`placed`/`sample_awaited` may receive a sample), the sample-type
vocabulary and the QC verdict vocabulary all live in the domain methods,
behind their own role gate, and NONE of them is repeated here. That is the
wave's hard constraint read literally: there is an existing workflow state and
there are existing transition verbs, and a second engine beside them is the
thing not to build.

WHY `/lab` AND NOT `/admin`. The prefix names the surface so it is visibly
laboratory-operational in a route census and in an access log — the same
reason `admin_endpoints.py` gives for `/admin`. It is not an authorisation
statement: the gate is `_require_role(session, "Lab technician")`, inside the
domain method, and `/admin/audit` already established that a prefix does not
dictate the role behind it (that one is Auditor, and retraction is Approver).

──────────────────────────────────────────────────────────────────────────
THE SECURITY MODEL, IN THE ORDER A REQUEST MEETS IT
──────────────────────────────────────────────────────────────────────────

1. AUTHENTICATION — `Depends(_get_session)`, IMPORTED from
   `clinical/endpoints.py` and not reimplemented. It is the same staff
   dependency the exception and administrative routes use, so every failure
   — absent, malformed, unknown, expired, terminated, or a PATIENT session id
   presented to a staff route — is `_anonymous_refusal()`: 401,
   `GENERIC_AUTH_ERROR`, byte for byte.

2. ORGANISATION CONTEXT — DERIVED, NEVER SUPPLIED. `Session.org_id` comes out
   of the `sessions` row. Neither route accepts an organisation in a path
   segment, a query parameter or a body field, and both request models set
   `extra="forbid"` so a body that tries to carry one is REFUSED rather than
   silently ignored. Both domain methods bind `org_id = session.org_id` in
   their own SQL, and `samples_order_fk` is a COMPOSITE foreign key
   `(order_id, org_id)`, so a sample referencing another organisation's order
   is not expressible even if that layer were wrong.

3. ROLE AUTHORISATION — `_require_role(session, "Lab technician")`, inside the
   domain method, as its FIRST EXECUTABLE STATEMENT. Not here. Because the
   gate precedes every target read, a 403 from this surface is a function of
   the CALLER'S ROLES ALONE and carries no information about whether the named
   order or sample exists, in this organisation or any other.

   THE TWO OPERATIONAL ROLES DO NOT OVERLAP, and `receive_sample`'s own
   docstring says so: an Orderer may order and may not receive; a Lab
   technician may receive and may not order. Administrator is not a superuser
   over either. Nothing here widens that.

4. AUDIT ATTRIBUTION — `@auditable` on the domain method, untouched. The actor
   is `session.user_id`, the organisation is `session.org_id`. Nothing in this
   file writes an audit row.

   AND NEITHER HANDLER COMPOSES TWO DECORATED METHODS. `@auditable` COMMITS
   after each audit row, so "do it, then read it back to return it" would
   commit the first half's audit row when the second half failed. Each handler
   calls EXACTLY ONE decorated domain method — which is why the QC route
   answers 204 and returns no sample body.

5. ERROR MODEL — THE SAME THREE REFUSALS AS THE ADMINISTRATIVE SURFACE, and
   they are IMPORTED rather than re-declared. `_forbidden`, `_not_found` and
   `_target` come from `clinical/admin_endpoints.py`; `ADMIN_FORBIDDEN` and
   `ADMIN_NOT_FOUND` are its constants. A second pair of strings on a second
   staff surface would let a caller tell WHICH surface refused it from the
   body, and would be a second refusal vocabulary to keep in step — the exact
   thing this house forbids. The names keep their `ADMIN_` spelling because
   renaming a constant two other files assert on would be a wider change than
   the one being made; they are the SERVICE'S refusal strings, not the
   administrative router's.

       401 + GENERIC_AUTH_ERROR   not authenticated, in any of its six ways
       403 + ADMIN_FORBIDDEN      authenticated, lacks Lab technician
       404 + ADMIN_NOT_FOUND      no such order/sample IN THIS ORGANISATION
       400 + the domain's message  a vocabulary or state refusal, AFTER the gate

   THE 400 IS NOT AN IDENTIFIER CHANNEL, and the check is the one
   `assign_role` and `retract_report` already made: every `ValueError` either
   domain method raises is raised strictly AFTER the role gate AND, for the
   state refusal, after the order has been found in the CALLER'S OWN
   organisation. The vocabulary messages name a closed public list of four
   sample types and two QC verdicts; the state message names the state of an
   order in the caller's own tenant. Neither names another tenant, another
   row, or an identifier the caller did not supply.

   NEITHER REQUEST MODEL VALIDATES ANYTHING, and that is load-bearing rather
   than laziness. Pydantic runs BEFORE the handler and therefore before the
   role gate. A `Literal["blood","saliva","tissue","dna"]` on `sample_type`
   would be a 422 answered to a caller holding no Lab technician role, so the
   refusal would vary with the CONTENT of the body at a point where the
   caller's authorisation had not been evaluated. That is hazard 5 —
   validation order leaks — and it is why both fields are plain `str` with no
   constraint and the vocabulary lives in the domain method, after the gate.

──────────────────────────────────────────────────────────────────────────
WHAT THIS SURFACE DOES NOT CLAIM — READ THIS BEFORE BELIEVING ANY OF IT
──────────────────────────────────────────────────────────────────────────

NEITHER ROUTE IS REACHABLE BY A REAL DEPLOYMENT TODAY, AND THE REASON IS
UPSTREAM OF THIS FILE RATHER THAN IN IT. Two separate facts, both measured at
`764d646`:

  * THE FIRST-ADMINISTRATOR DEADLOCK, unchanged by this wave.
    `provision_organisation` has ZERO production call sites outside
    `data_access.py`, so every role-holding session — Lab technician included
    — is exactly as reachable as an operator invoking it directly. Nothing in
    this file changes that in either direction.

  * THE ORDER STAGE HAS NO PRODUCTION ENTRY POINT AND IS BLOCKED ON AN ABSENT
    CAPABILITY. `receive_sample` requires an order in state `placed` or
    `sample_awaited`; `create_order` has zero production callers and zero
    routes; and `create_order` requires a `test_id` that is a foreign key into
    `tests`, a table for which NO `DataAccess` method of any kind exists —
    measured over every method of the class by syntax tree. Who may define a
    test panel, its CDSCO class and its gene list is a product decision nobody
    has made, so the Order stage is STOPPED AND CARDED rather than guessed,
    and these two routes therefore have no subject a production path can
    create yet.

So both routes are IMPLEMENTED + UNREACHABLE in the report's own vocabulary,
and they are classified that way rather than as complete. Their tests
provision the Lab technician BY INSERTING THE ROLE ROW DIRECTLY and build the
order through the domain methods — a FIXTURE, not a bootstrap. No bootstrap
and no test-catalogue writer was invented here to make them pass.

WHAT IS DELIBERATELY NOT HERE, AND WHY — each is a card, not an oversight.

* THE ORDER STAGE (`create_order`, `place_order`, `cancel_order`). All three
  are Orderer-gated and complete; all three have zero production callers.
  `place_order` and `cancel_order` are mechanically pure wiring. They are NOT
  wired here because a route onto either would act on an order that no
  production path can create, for the `tests`-catalogue reason above, and
  mounting the middle of a severed chain without its head would report
  progress the chain does not have. Carded WITH the decision required.

* PROCESSING (`create_sequencing_run`, `create_vcf`) and INTERPRETATION
  (`create_interpretation`, `create_report`). `record_pipeline_result` already
  performs all four inserts in ONE `@transactional` body — deliberately, and
  its docstring says why: the four decorated methods called in a row are four
  commits, and a failure at the third leaves a VCF with no interpretation. It
  is reachable from `geper/api/submission_worker.py`. Routes onto the four
  individually would be a second way to build the same lineage, non-atomic,
  beside the one that exists. AND `create_interpretation` and `create_report`
  contain NO `_require_role` call anywhere in their bodies — AST-confirmed —
  so a route onto either would need an authorisation rule invented at the HTTP
  layer. STOPPED.

* RELEASE (`_release_report`, `require_release`). THE HIGHEST-CONSEQUENCE GAP
  IN THE CHAIN AND THE ONE THIS WAVE REFUSED TO CLOSE. Re-verified at
  `764d646`: ZERO production call sites, by the alias-resolving census above
  and with a control needle that the instrument does find. The consequence is
  on the record and is unchanged — `list_my_released_reports` requires BOTH
  `reports.state = 'released'` AND a `release_events` row, so
  `/portal/me/reports`, `/portal/me/reports/{report_id}` and
  `/portal/me/trends` return empty for every patient for ever, and Wave 6's
  retraction verb acts on a state nothing in production can produce.

  WHO may release and FROM WHICH STATE are both derivable and already encoded:
  Approver on the session AND on the recorded `released_by` (w129 phase B),
  from `approved` or `released` (spec 13.3/13.4). WHAT IS NOT DERIVABLE is the
  `consumer`, and the tree says so ITSELF, in `schema.sql`'s own comment on
  the column:

      -- The downstream recipient (spec 13.4: a clinician, a patient, a LIMS,
      -- any consumer). Free text pending the delivery paths of a later commit;
      -- enumerating it now would fix a vocabulary before the paths exist.

  Spec 13.4 describes release as an event a DELIVERY PATH records — "every
  delivery path checks approval ... there is one function every delivery path
  calls" — and names no route and no trigger. There is no delivery path in
  this backend. So a `POST .../release` taking a caller-supplied consumer
  string would settle by accident the exact question the schema author
  deferred on purpose, and would additionally decide that release is a
  standalone administrative act rather than a consequence of delivery, and
  that a patient's portal access is granted by a staff member typing a string.
  Three product decisions. STOPPED AND CARDED — see the report for the exact
  decision required.

* AMENDMENT (`create_amendment`) and INTEGRITY VERIFICATION
  (`verify_report_integrity`). Approver-gated, complete, zero production
  callers. Both act on an APPROVED report, and nothing in production approves
  one except the review CLI. Out of this wave's two wired stages; carded.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from clinical.admin_endpoints import _forbidden, _not_found, _target
from clinical.data_access import AuthorizationError, DataAccess, NotFoundError, Session
from clinical.endpoints import _get_data_access, _get_session

# PREFIXED, so that every bench operation is visibly laboratory-operational in
# the route census and in any access log, and so that a reverse proxy or a
# future network policy can speak about the whole surface in one rule. It is
# not an authorisation statement -- see the module docstring.
router = APIRouter(prefix="/lab", tags=["laboratory"])


# ─── request and response models ─────────────────────────────────────────────
#
# `extra="forbid"` ON EVERY REQUEST MODEL, and NO VALUE CONSTRAINT ON ANY
# FIELD. The first is so that a body trying to carry an organisation is
# REFUSED rather than silently ignored; the second is because Pydantic runs
# before the role gate and a value check here would answer a caller whose
# authorisation has not been evaluated. Both rules are the administrative
# surface's, unchanged.


class SampleReceiptRequest(BaseModel):
    """
    The body of `POST /lab/orders/{order_id}/samples`.

    THERE IS NO `org_id` FIELD, NO `order_id` FIELD, NO `collected_by` FIELD
    AND NO `received_by` FIELD, and all four absences are the design. The
    order is the PATH TARGET, resolved inside the caller's own organisation;
    the organisation is `Session.org_id`; and BOTH provenance stamps --
    `samples.collected_by` and `samples.received_by` -- are `session.user_id`,
    written by `receive_sample` itself. A caller-supplied receiver would be a
    claim, and `ResolutionRequest` in `clinical/endpoints.py` already settled
    for this repository that such a field is REMOVED rather than validated:
    under ISO 15189 the record of who performed an action is evidence, and a
    claimed actor is indistinguishable in the log from a verified one.

    `sample_type` IS `str` AND IS NOT CONSTRAINED HERE. The four-value
    vocabulary is checked by `receive_sample`, after the gate. A
    `Literal[...]` would be a 422 answered pre-gate -- see the module
    docstring, hazard 5.
    """

    model_config = ConfigDict(extra="forbid")

    sample_type: str
    # Nullable in the schema and optional in the domain method: a specimen
    # that arrived with nothing worth noting is a real outcome, and a required
    # field would force a sentence into existence to describe it.
    condition_on_receipt: Optional[str] = None


class SampleReceived(BaseModel):
    """
    The sample's identifier, and nothing else.

    NOT the order, not the type, not the timestamps -- not because a Lab
    technician may not see them (they may; they just sent two of them), but
    because a response that re-derived them would be a second view of a row
    the caller can already read, and this surface renders rather than
    interprets. It is also the handle the QC route below takes, which is the
    only thing the caller actually needs next.
    """

    sample_id: str


class QcRequest(BaseModel):
    """
    The body of `POST /lab/samples/{sample_id}/qc`.

    THERE IS NO `org_id` FIELD, NO `sample_id` FIELD AND NO
    `qc_recorded_by` FIELD, for the reasons `SampleReceiptRequest` gives. The
    verdict's author is `session.user_id`, written to `samples.qc_recorded_by`
    by `record_qc` itself.

    `qc_status` IS `str` AND IS NOT CONSTRAINED HERE. `record_qc` accepts
    exactly `'passed'` and `'failed'` -- NOT `'pending'`, which is the column's
    default and a state rather than a verdict -- and it refuses anything else
    after the gate. The column's own CHECK admits all three, so the narrower
    rule is the domain method's and this file does not restate it.
    """

    model_config = ConfigDict(extra="forbid")

    qc_status: str
    # Optional because a passing verdict usually has nothing to say. The
    # domain method stores whatever arrives, including None.
    qc_reason: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: str


# ─── the routes ──────────────────────────────────────────────────────────────


@router.post(
    "/orders/{order_id}/samples",
    status_code=status.HTTP_201_CREATED,
    response_model=SampleReceived,
    summary="Record receipt of a specimen against an order in the CALLER'S OWN organisation (Lab technician)",
    responses={
        201: {"description": "The sample is recorded and the order advanced if it was 'placed'"},
        400: {"model": ErrorResponse, "description": "Unknown sample type, or the order cannot receive a sample"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def receive_sample(
    order_id: str,
    body: SampleReceiptRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> SampleReceived:
    """
    `DataAccess.receive_sample`, and nothing else.

    LAB TECHNICIAN, NOT ADMINISTRATOR. The gate is the domain method's,
    unchanged, and it is that method's first executable statement.

    NO CONDITION IS REPEATED HERE. The Lab technician check, the four-value
    sample-type vocabulary, the "order must be `placed` or `sample_awaited`"
    rule, the `placed -> sample_awaited` advance and every organisation
    predicate are in the domain method. If this handler re-checked the order
    state there would be two answers to "may this order receive a sample", and
    the day they disagreed the HTTP one would win silently.

    THE ORDER ADVANCE IS THE EXISTING STATE MACHINE AND IS NOT A SECOND ONE.
    `receive_sample` moves `orders.state` from `placed` to `sample_awaited`
    itself, inside its own `@transactional` body and in the same transaction
    as the INSERT. This handler does not touch `orders`, does not call a
    second verb to advance it, and does not know the transition exists. A
    handler that wrote the end state directly would bypass the very check that
    makes the transition legal.

    `order_id` GOES THROUGH `_target`: a malformed identifier becomes one that
    names nothing, so it takes the same path as an id that exists nowhere and
    the role gate still runs first. See that function's docstring for why it
    must not raise.
    """
    target = _target(order_id)
    try:
        sample_id = dao.receive_sample(session, target, body.sample_type, body.condition_on_receipt)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return SampleReceived(sample_id=str(sample_id))


@router.post(
    "/samples/{sample_id}/qc",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record the QC verdict for a specimen in the CALLER'S OWN organisation (Lab technician)",
    responses={
        204: {"description": "The verdict is recorded"},
        400: {"model": ErrorResponse, "description": "The QC status is not 'passed' or 'failed'"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def record_qc(
    sample_id: str,
    body: QcRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    """
    `DataAccess.record_qc`, and nothing else.

    204 AND NO BODY, AND THAT IS THE ATOMICITY RULE RATHER THAN A STYLE
    CHOICE. `record_qc` returns None, so a response carrying the updated
    sample would have to READ IT BACK through a second decorated method --
    `get_sample` -- and `@auditable` COMMITS after each audit row. The
    `qc_recorded` row would then be committed by the first call and a failure
    in the second would return an error for an operation that had already
    landed. One decorated call per handler; the caller already knows the
    verdict it sent.

    NO CONDITION IS REPEATED HERE. The Lab technician check, the
    `'passed'`/`'failed'` vocabulary and the organisation predicate are in the
    domain method.

    QC IS A VERDICT ON A SPECIMEN, NOT A TRANSITION OF THE ORDER, and this
    route does not make it one. `record_qc` writes four columns on `samples`
    and touches `orders` not at all; nothing downstream of it in this file
    advances anything. Whether a failed QC should cancel or hold the order is
    a workflow question this wave did not answer and did not assume -- the
    exception workflow (`create_or_reopen_exception`, reason codes owned by
    `lab_operator`) is the mechanism that already exists for it.
    """
    target = _target(sample_id)
    try:
        dao.record_qc(session, target, body.qc_status, body.qc_reason)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Milestone 1: the staff READ surface for samples ────────────────────────────────────────────────
#
# Read-only, organisation-scoped, role-gated inside the domain method (Lab technician, Orderer, Interpreter,
# Approver, Auditor) and AUDITED with no payload. List parameters are `Optional[str]` for the reason given with
# the clinic read routes: validation must follow the role gate. The staff user ids on a sample are never shown.


def _iso(value):
    return None if value is None else value.isoformat()


class SampleView(BaseModel):
    """A sample as staff see it. `qc_reason` (free text) is None in every list, and on a detail read unless the
    caller is a Lab technician, Orderer, Interpreter or Approver."""

    sample_id: str
    order_id: str
    type: str
    received_at: Optional[str] = None
    condition_on_receipt: Optional[str] = None
    qc_status: Optional[str] = None
    qc_recorded_at: Optional[str] = None
    created_at: str
    qc_reason: Optional[str] = None


class SamplePage(BaseModel):
    items: list[SampleView]
    total: int
    limit: int
    offset: int


def _sample_view(row):
    return SampleView(
        sample_id=str(row["sample_id"]),
        order_id=str(row["order_id"]),
        type=row["type"],
        received_at=_iso(row["received_at"]),
        condition_on_receipt=row["condition_on_receipt"],
        qc_status=row["qc_status"],
        qc_recorded_at=_iso(row["qc_recorded_at"]),
        created_at=_iso(row["created_at"]),
        qc_reason=row["qc_reason"],
    )


@router.get(
    "/samples",
    response_model=SamplePage,
    summary="List the caller's own organisation's samples (Lab technician, Orderer, Interpreter, Approver, Auditor)",
    responses={
        400: {"model": ErrorResponse, "description": "A list parameter is malformed (checked after the role gate)"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
    },
)
async def list_samples(
    order_id: Optional[str] = None,
    qc_status: Optional[str] = None,
    limit: Optional[str] = None,
    offset: Optional[str] = None,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> SamplePage:
    """`DataAccess.list_samples`, and nothing else. Newest first. A list never carries `qc_reason`. A malformed
    `order_id` names nothing and returns an empty page."""
    try:
        page = dao.list_samples(session, order_id=order_id, qc_status=qc_status, limit=limit, offset=offset)
    except AuthorizationError:
        raise _forbidden()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return SamplePage(
        items=[_sample_view(r) for r in page["items"]], total=page["total"], limit=page["limit"], offset=page["offset"]
    )


@router.get(
    "/samples/{sample_id}",
    response_model=SampleView,
    summary="One sample of the caller's own organisation (Lab technician, Orderer, Interpreter, Approver, Auditor)",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found (absent, malformed or another organisation's)"},
    },
)
async def get_sample(
    sample_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> SampleView:
    """`DataAccess.read_sample`, and nothing else."""
    target = _target(sample_id)
    try:
        row = dao.read_sample(session, target)
    except AuthorizationError:
        raise _forbidden()
    if row is None:
        raise _not_found()
    return _sample_view(row)
