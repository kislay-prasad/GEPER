"""
clinical/clinic_endpoints.py
────────────────────────────
w162 — THE ORDERING CLINICIAN'S SURFACE. Patient registration and consent,
and nothing else.

WHY THIS FILE EXISTS, STATED AS THE MEASUREMENT AND NOT AS A STORY.
Measured at `w126m-migrations` tip `44d624b` by an `ast.Call` census over
every non-test module in the repository — never grep, which credits a
docstring as a caller:

    create_patient        0 production call sites
    record_consent        0 production call sites
    withdraw_consent      0 production call sites

and the only production writer of a `patients` row is `create_patient`
itself (`INSERT INTO patients`, data_access.py:2370), while the only
production writer of a `consents` row is `record_consent` (the INSERT and
the supersession UPDATE) and `withdraw_consent` (the withdrawal UPDATE).
There is no second path to either table. So the three verbs were the whole
mechanism and NOT ONE of them was reachable over HTTP, and — until the
commit this module arrives with — not one of them enforced a role either.

THE RULING THIS WAVE IMPLEMENTS (human, 2026-09-19):

    PATIENT REGISTRATION + CONSENT — the ORDERER/CLINICIAN owns both.
    Do NOT make Administrator registration mandatory. Do NOT make Lab
    Technician a patient identity creator.

"ORDERER" IS THE TOKEN, AND IT IS READ OFF THE TREE RATHER THAN CHOSEN.
`data_access.ROLES` is the vocabulary the schema's CHECK constraint on
`role_assignments.role` enumerates: Orderer, Lab technician, Interpreter,
Approver, Administrator, Auditor, System. Exactly one of those names a
clinician, and `create_order`'s own docstring already says which and why —
"Ordering a test is the clinical decision the Orderer role names". The
ruling's "ordering clinician" and the vocabulary's "Orderer" are the same
principal, and registration and consent are now gated on it, INSIDE the
three domain methods, as each one's first executable statement.

CLINICIAN-ONLY, AND ADMINISTRATOR IS NOT ADDED ALONGSIDE. The ruling
forbids MANDATORY Administrator registration; it does not say whether an
Administrator may ALSO register. The tree does not answer it either, and it
answers the adjacent question in the narrow direction: `create_order`'s
docstring records that "an Administrator is not implicitly an Orderer,
because the ruling gives Administrator organisation/staff administration
and a role that silently subsumes the others is the RBAC redesign the same
ruling forbids". So this wave implements the NARROWER reading — Orderer
alone — and the widening is carded rather than assumed. Widening a gate
later is a one-line change with a test; narrowing one after deployment is
a capability withdrawal.

WHY A THIRD ROUTER AND NOT `/admin` OR `/lab`.
The house's own rule, stated in both existing surfaces, is that the prefix
NAMES THE SURFACE and is not an authorisation statement (`/admin/audit` is
Auditor-gated, `/admin/reports/{id}/retraction` is Approver-gated). Read
that way, neither existing prefix fits:

  * `/admin` names the ADMINISTRATIVE surface. Filing clinician-owned
    registration under it would encode in the URL — in every route census,
    every access log and every reverse-proxy rule — precisely the reading
    the ruling rejects: that registering a patient is an administrative act.
  * `/lab` names the LABORATORY BENCH, and its module docstring says so:
    the prefix exists "so that every bench operation is visibly
    laboratory-operational". Registration and consent are not bench
    operations, and the ruling names Lab Technician as the role that must
    NOT create a patient identity.

`/clinic` names the third surface this service has: the ordering
clinician's. It is also where the ORDER stage belongs when the `tests`
catalogue unblocks it — `create_order`, `place_order` and `cancel_order`
are Orderer-gated, complete, and have zero production callers, and they are
deliberately NOT here (see WHAT IS NOT HERE, below).

NO SECOND ANYTHING. `_get_session` and `_get_data_access` are IMPORTED from
`clinical/endpoints.py`; `_forbidden`, `_not_found` and `_target` and their
two body constants are IMPORTED from `clinical/admin_endpoints.py`. This
module defines no dependency, no refusal string, no SQL and no role check.

──────────────────────────────────────────────────────────────────────────
THE SECURITY MODEL, IN THE ORDER A REQUEST MEETS IT
──────────────────────────────────────────────────────────────────────────

1. AUTHENTICATION — `Depends(_get_session)`, the same staff dependency the
   administrative, exception and laboratory routes use. Every failure —
   absent, malformed, unknown, expired, terminated, or a PATIENT session id
   presented to a staff route — is `_anonymous_refusal()`: 401,
   `GENERIC_AUTH_ERROR`, byte for byte.

2. ORGANISATION CONTEXT — DERIVED, NEVER SUPPLIED. `Session.org_id` comes
   out of the `sessions` row. No route here accepts an organisation in a
   path segment, a query parameter or a body field, and every request model
   sets `extra="forbid"`, so a body that tries to carry one is REFUSED
   rather than silently ignored. `create_patient` binds `org_id =
   session.org_id` in its INSERT; `record_consent` and `withdraw_consent`
   bind `AND org_id = %s` from the session on every read and every write.

3. ROLE AUTHORISATION — `_require_role(session, "Orderer")`, INSIDE the
   domain method, as its FIRST EXECUTABLE STATEMENT. Not here. Because the
   gate precedes every target read AND every vocabulary check, a 403 from
   this surface is a function of the CALLER'S ROLES ALONE: it carries no
   information about whether the named patient or consent exists, in this
   organisation or any other, and none about whether the body was
   well-formed.

4. AUDIT ATTRIBUTION — `@auditable` on the domain method, untouched. The
   actor is `session.user_id`, the organisation is `session.org_id`, and
   `consents.recorded_by` / `consents.withdrawn_by` take the same value
   from the same place. Nothing in this file writes an audit row, and
   nothing in this file lets a caller name an actor.

   AND NO HANDLER COMPOSES TWO DECORATED METHODS. `@auditable` COMMITS
   after each audit row, so "register the patient, then record the consent"
   as one handler would commit `patient_created` and leave a patient with
   no consent when the second half failed. Each handler calls EXACTLY ONE
   decorated domain method — which is also why the withdrawal route answers
   204 and returns no consent body.

5. ERROR MODEL — THE SAME REFUSALS AS THE OTHER TWO STAFF SURFACES, and
   they are IMPORTED rather than re-declared:

       401 + GENERIC_AUTH_ERROR   not authenticated, in any of its ways
       403 + ADMIN_FORBIDDEN      authenticated, lacks Orderer
       404 + ADMIN_NOT_FOUND      no such patient/consent IN THIS ORGANISATION
       400 + the domain's message a vocabulary or format refusal, AFTER the gate

   NO REQUEST MODEL CONSTRAINS A VALUE, and that is load-bearing rather
   than laziness. Pydantic runs BEFORE the handler and therefore before the
   role gate. A `Literal["M","F","O","U"]` on `sex`, or a `datetime.date`
   on `dob`, would be a 422 answered to a caller holding no Orderer role,
   so the refusal would vary with the CONTENT of the body at a point where
   the caller's authorisation had not been evaluated. Every field here is
   `str`, and BOTH vocabularies — the four sex codes and the ISO-8601 date
   format — are checked by `create_patient`, after the gate.

   THE DATE IS THE ONE THAT HAD TO MOVE. `create_patient` took
   `dob: datetime.date` and could not be handed a string, so the parse
   would have had to happen in this handler, before the gate, exactly like
   the malformed-identifier defect `_target` was rewritten to remove. It
   could not take `_target`'s fix either: substituting a fresh random value
   is right for an IDENTIFIER, which selects nothing when it is wrong, and
   catastrophic for a DATE OF BIRTH, which would be silently recorded. So
   `create_patient` now ACCEPTS EITHER a `date` or an ISO-8601 string and
   parses the string itself, after its own role gate, raising `ValueError`
   the way the `sex` check already does. The vocabulary moved to where the
   house puts vocabularies; nothing about the existing `date` callers
   changed.

   THE 400 IS NOT AN IDENTIFIER CHANNEL. Every `ValueError` these methods
   raise is raised strictly AFTER the role gate, and both messages name
   only a closed public vocabulary or the caller's own submitted value —
   never another tenant, another row, or an identifier the caller did not
   supply.

──────────────────────────────────────────────────────────────────────────
THE SHAPE OF EACH ROUTE, AND WHY IT IS SAFE
──────────────────────────────────────────────────────────────────────────

The house has two shapes and every route must be classified as one:

  * DEFENCE BY ABSENT PARAMETER — the portal's `list_my_report_trend(self,
    patient_session)` takes no patient identifier at all, so the wrong
    version is unwritable. That is the right shape when the caller IS the
    subject.
  * AN AUTHENTICATED STAFF PRINCIPAL NAMING A TARGET — legitimate because
    the caller is authenticated, the target is resolved INSIDE the caller's
    own organisation, and the organisation comes from the session.

    POST /clinic/patients
        NEITHER SHAPE'S TARGET PROBLEM ARISES: there is no target. The
        patient does not exist yet, the identifier is MINTED by
        `create_patient` (`uuid.uuid4()`), and the organisation is
        `session.org_id`. Nothing in the request can name a patient or an
        organisation. This is the strongest form of the first shape.

    POST /clinic/patients/{patient_id}/consents
    POST /clinic/consents/{consent_id}/withdrawal
        THE SECOND SHAPE. Both act on a row other than the caller, so there
        is an identifier and it is in the PATH. What makes that safe is not
        the identifier's absence but where its ORGANISATION comes from:
        both domain methods bind `AND org_id = %s` from `session.org_id`,
        so a real patient or a real consent in ANOTHER tenant reads exactly
        like an invented one and the refusal is byte-identical. The
        identifier selects a row inside the caller's tenant; it never
        selects a tenant.

REGISTRATION AND CONSENT ARE TWO VERBS, NOT ONE, AND THE TREE DECIDED THAT
BEFORE THIS WAVE. `consents.patient_id` is a foreign key, so the patient
must exist first; `create_order` takes an ALREADY-RECORDED `consent_id`
and re-reads it, so consent is a standing record rather than a step of
registration; and the two acts have two audit actions (`patient_created`,
`consent_recorded`) against two resource types. The ruling requires
"transaction atomicity" and does not require them to be ONE act, so the
existing shape is kept and each verb is INDIVIDUALLY atomic: one
`@transactional` body under one `@auditable`, with no partial state
reachable. A combined verb would have to be a new domain method composing
two decorated ones — which is the `@auditable` composition hazard in its
textbook form — and nobody asked for it.

──────────────────────────────────────────────────────────────────────────
WHAT THIS SURFACE DOES NOT CLAIM
──────────────────────────────────────────────────────────────────────────

THE FIRST-ADMINISTRATOR DEADLOCK IS UNCHANGED BY THIS WAVE.
`provision_organisation` still has ZERO production call sites outside
`data_access.py`, and `assign_role` still requires an existing
Administrator. So an Orderer session is exactly as reachable as an
operator invoking `provision_organisation` directly, and these three
routes are IMPLEMENTED + UNREACHABLE-BY-A-FRESH-DEPLOYMENT in the same
sense every administrative and laboratory route already is. What has
changed, and it is the whole point of the wave, is that they are no longer
unreachable BY DESIGN: given a roled session, a clinician can now register
a patient and record consent over HTTP, which was not true at `44d624b`.

WHAT IS NOT HERE, AND WHY — each is a card, not an oversight.

* THE ORDER STAGE (`create_order`, `place_order`, `cancel_order`). All
  three are Orderer-gated and complete; all three have zero production
  callers; and all three would belong on THIS prefix. They are not wired
  because `create_order` requires a `test_id` foreign key into `tests`, a
  table for which NO `DataAccess` method of any kind exists — so no
  production path can create the subject of the route. Who may define a
  test panel is a product decision nobody has made. Carded, unchanged from
  w157.

* A READ SURFACE. `get_patient` and `get_consent` exist, are session-bound
  and are `auditable=False` reads. This wave adds WRITE reachability for
  the three verbs the ruling names and does not invent a patient-search or
  patient-list route: who may enumerate patients, and on what predicate, is
  a question the ruling did not answer.

* PATIENT AMENDMENT OR DELETION. No domain method exists for either.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict

from clinical.admin_endpoints import _forbidden, _not_found, _target
from clinical.data_access import AuthorizationError, DataAccess, NotFoundError, Session
from clinical.endpoints import _get_data_access, _get_session

# PREFIXED, so that every clinician-facing act is visibly so in the route
# census and in any access log, and so that a reverse proxy or a future
# network policy can speak about the whole surface in one rule. It is not an
# authorisation statement -- see the module docstring.
router = APIRouter(prefix="/clinic", tags=["clinic"])


# ─── request and response models ─────────────────────────────────────────────
#
# `extra="forbid"` ON EVERY REQUEST MODEL, and NO VALUE CONSTRAINT AND NO
# NON-`str` TYPE ON ANY FIELD A VOCABULARY GOVERNS. The first is so that a
# body trying to carry an organisation is REFUSED rather than silently
# ignored; the second is because Pydantic runs before the role gate and any
# check here would answer a caller whose authorisation has not been
# evaluated.


class PatientRegistrationRequest(BaseModel):
    """
    The body of `POST /clinic/patients`.

    THERE IS NO `org_id` FIELD, NO `patient_id` FIELD AND NO `created_by`
    FIELD, and all three absences are the design. The organisation is
    `Session.org_id`; the identifier is MINTED by `create_patient`; and the
    registering actor is `session.user_id`, recorded by `@auditable`. A
    caller-supplied identifier would let a request name a row, and a
    caller-supplied actor would be a claim — the rule this repository
    already settled (`ResolutionRequest` in `clinical/endpoints.py`) is that
    such a field is REMOVED rather than validated.

    `dob` IS `str` AND NOT `datetime.date`, AND THAT IS THE LOAD-BEARING
    ONE. A `datetime.date` annotation makes Pydantic refuse `"not-a-date"`
    with a 422 BEFORE the handler runs and therefore before the role gate,
    so a caller holding no Orderer role would get a different answer for a
    malformed body than for a well-formed one. The ISO-8601 parse lives in
    `create_patient`, after its gate, beside the `sex` vocabulary it already
    enforced there.

    `sex` IS `str` AND IS NOT CONSTRAINED HERE for the same reason: the
    four-value vocabulary `M`/`F`/`O`/`U` is `create_patient`'s and this
    file does not restate it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    dob: str
    sex: str


class PatientRegistered(BaseModel):
    """The patient's identifier, and nothing else.

    NOT the name, the date of birth or the sex — not because a clinician may
    not see them (they just sent all three), but because a response that
    echoed them would be a second view of a row the caller can already read,
    and this surface renders rather than interprets. It is also the handle
    the consent route below takes.
    """

    patient_id: str


class ConsentRequest(BaseModel):
    """
    The body of `POST /clinic/patients/{patient_id}/consents`.

    THERE IS NO `org_id` FIELD, NO `patient_id` FIELD AND NO `recorded_by`
    FIELD. The patient is the PATH TARGET, resolved inside the caller's own
    organisation; the organisation is `Session.org_id`; the recorder is
    `session.user_id`, written to `consents.recorded_by` by `record_consent`
    itself.

    `scope` IS `str` AND IS NOT CONSTRAINED, and here that is a statement
    about the TREE rather than only about validation order: `consents.scope`
    carries no CHECK constraint and no vocabulary exists anywhere in the
    repository for it — `create_order` compares it for EQUALITY against the
    order's `required_scope` and nothing enumerates the values. Inventing a
    scope vocabulary at the HTTP layer would settle a product question by
    implementation, on the surface furthest from the data. STOPPED AND
    CARDED; the field is passed through exactly as the domain method takes
    it.

    `supersedes_id` IS OPTIONAL AND IS A PATH-STYLE IDENTIFIER IN THE BODY,
    because it names a SIBLING of the row being created rather than the
    route's target. It goes through `_target` for the same reason path
    identifiers do: a malformed value must take the same path as one that
    names nothing, so the role gate still runs first. `record_consent`'s
    existing behaviour for a supersedes id that matches no active consent of
    this patient in this organisation is to record the new consent and
    leave the pointer unset — unchanged by this wave, and noted here
    because it is the one place where a wrong value is not refused.
    """

    model_config = ConfigDict(extra="forbid")

    scope: str
    supersedes_id: Optional[str] = None


class ConsentRecorded(BaseModel):
    """The consent's identifier, which is also the handle the withdrawal
    route takes and the value `create_order` will require. No patient, no
    scope, no timestamp — nothing the caller did not send or already hold."""

    consent_id: str


class ErrorResponse(BaseModel):
    detail: str


# ─── the routes ──────────────────────────────────────────────────────────────


@router.post(
    "/patients",
    status_code=status.HTTP_201_CREATED,
    response_model=PatientRegistered,
    summary="Register a patient in the CALLER'S OWN organisation (Orderer)",
    responses={
        201: {"description": "The patient is registered and the registering clinician is recorded"},
        400: {"model": ErrorResponse, "description": "Unknown sex code, or a date of birth that is not ISO-8601"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
    },
)
async def register_patient(
    body: PatientRegistrationRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> PatientRegistered:
    """
    `DataAccess.create_patient`, and nothing else.

    ORDERER, NOT ADMINISTRATOR AND NOT LAB TECHNICIAN. The gate is the
    domain method's, unchanged by this handler, and it is that method's
    first executable statement.

    THERE IS NO 404 IN THIS ROUTE'S SET, and that is a property rather than
    an omission: registration names no existing row, so there is nothing to
    fail to find. The absence is what makes the route's refusal set
    {401, 403, 400} and nothing else.

    NO CONDITION IS REPEATED HERE. The Orderer check, the `M`/`F`/`O`/`U`
    vocabulary, the ISO-8601 date parse and the organisation binding are all
    in the domain method. If this handler parsed the date there would be two
    answers to "is this a date", and the day they disagreed the HTTP one
    would win — before the gate had run.
    """
    try:
        patient_id = dao.create_patient(session, body.name, body.dob, body.sex)
    except AuthorizationError:
        raise _forbidden()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return PatientRegistered(patient_id=str(patient_id))


@router.post(
    "/patients/{patient_id}/consents",
    status_code=status.HTTP_201_CREATED,
    response_model=ConsentRecorded,
    summary="Record consent for a patient in the CALLER'S OWN organisation (Orderer)",
    responses={
        201: {"description": "The consent is recorded and the recording clinician is recorded with it"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def record_consent(
    patient_id: str,
    body: ConsentRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ConsentRecorded:
    """
    `DataAccess.record_consent`, and nothing else.

    THE SHAPE IS "AN AUTHENTICATED CLINICIAN NAMING A TARGET", which is the
    second of the two shapes the module docstring sets out. It could not be
    the first: consent is recorded FOR a patient other than the caller, so
    there is a patient identifier and it is in the path. What makes that
    safe is that `record_consent` resolves it with `WHERE patient_id = %s
    AND org_id = %s`, `org_id` from the session — a real patient in another
    tenant reads exactly like an invented one and the refusal is
    byte-identical.

    THE GATE RUNS FIRST, so the 403 a non-Orderer receives is identical
    whether the named patient is real, invented, or in another tenant.

    EXACTLY ONE DECORATED CALL. A handler that read the consent back, or
    that registered the patient first, would commit the first audit row and
    then be able to fail with the record already written.

    `patient_id` AND `supersedes_id` BOTH GO THROUGH `_target`: a malformed
    identifier becomes one that names nothing, so it takes the same path as
    an id that exists nowhere and the role gate still runs first. See that
    function's docstring for why it must not raise.
    """
    target = _target(patient_id)
    supersedes = _target(body.supersedes_id) if body.supersedes_id is not None else None
    try:
        consent_id = dao.record_consent(session, target, body.scope, supersedes)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    return ConsentRecorded(consent_id=str(consent_id))


@router.post(
    "/consents/{consent_id}/withdrawal",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw an active consent in the CALLER'S OWN organisation (Orderer)",
    responses={
        204: {"description": "The consent is withdrawn and the withdrawing clinician is recorded"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def withdraw_consent(
    consent_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    """
    `DataAccess.withdraw_consent`, and nothing else.

    204 AND NO BODY, AND THAT IS THE ATOMICITY RULE RATHER THAN A STYLE
    CHOICE. `withdraw_consent` returns None, so a response carrying the
    withdrawn consent would have to READ IT BACK through a second decorated
    method — and `@auditable` COMMITS after each audit row, so the
    `consent_withdrawn` row would be committed by the first call and a
    failure in the second would return an error for an act that had already
    landed. One decorated call per handler.

    A POST TO A `/withdrawal` SUB-RESOURCE RATHER THAN A `DELETE` ON THE
    CONSENT, because withdrawal is not deletion: the row stays, `withdrawn_at`
    and `withdrawn_by` are stamped on it, and the consent remains readable
    as the historical fact it is. A `DELETE` would name the wrong act, and
    an audit trail whose verb says "delete" for a record that survives is a
    log that misdescribes itself.

    ALREADY-WITHDRAWN IS A 404 AND NOT A 409, and that is the domain
    method's answer, not a choice made here: `withdraw_consent` selects
    `WHERE ... AND withdrawn_at IS NULL`, so a consent that has already been
    withdrawn is simply not found among the active ones. Folding it into a
    distinct status would tell a caller that a consent it may not act on
    nevertheless exists.
    """
    target = _target(consent_id)
    try:
        dao.withdraw_consent(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── the catalogue read and the order stage (stage 2) ────────────────────────
#
# `create_order` / `place_order` / `cancel_order` were complete, Orderer-gated
# and unreachable for want of a test catalogue. Each route is exactly one
# decorated call. No body carries an organisation, an actor or a state: the
# organisation is Session.org_id, the orderer is session.user_id, and the
# state is the domain method's.


class TestListItem(BaseModel):
    test_id: str
    name: str
    cdsco_class: Optional[str]
    assembly: str
    status: str
    genes: list[str]


class OrderRequest(BaseModel):
    """The body of `POST /clinic/orders`. `patient_id`, `test_id` and
    `consent_id` are strings parsed with `_target` so a malformed value takes
    the path of one that names nothing and the role gate still runs first."""

    model_config = ConfigDict(extra="forbid")

    patient_id: str
    test_id: str
    consent_id: str
    required_scope: str
    # `Optional[str] = None` and NOT `str = "routine"`: this surface's rule
    # (w162) is that no request model carries a default other than None, so
    # that a value rule is never answered before the role gate. The default
    # priority is `create_order`'s own and is applied by omitting the argument.
    priority: Optional[str] = None
    clinical_indication: Optional[str] = None


class OrderCreated(BaseModel):
    order_id: str


@router.get(
    "/tests",
    response_model=list[TestListItem],
    summary="The caller's organisation's active test catalogue (Orderer or Administrator)",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
    },
)
async def list_tests(
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> list[TestListItem]:
    try:
        rows = dao.list_tests(session)
    except AuthorizationError:
        raise _forbidden()
    return [
        TestListItem(
            test_id=str(r["test_id"]),
            name=r["name"],
            cdsco_class=r["cdsco_class"],
            assembly=r["assembly"],
            status=r["status"],
            genes=r["genes"],
        )
        for r in rows
    ]


@router.post(
    "/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderCreated,
    summary="Create a draft order for a patient of the caller's own organisation (Orderer)",
    responses={
        201: {"description": "The draft order is created and the ordering clinician is recorded"},
        400: {"model": ErrorResponse, "description": "Bad priority, or consent scope does not match"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Patient, active test or active consent not found"},
    },
)
async def create_order(
    body: OrderRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> OrderCreated:
    """`DataAccess.create_order`, and nothing else."""
    patient = _target(body.patient_id)
    test = _target(body.test_id)
    consent = _target(body.consent_id)
    try:
        optional = {} if body.priority is None else {"priority": body.priority}
        order_id = dao.create_order(
            session,
            patient,
            test,
            body.required_scope,
            consent,
            clinical_indication=body.clinical_indication,
            **optional,
        )
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return OrderCreated(order_id=str(order_id))


@router.post(
    "/orders/{order_id}/placement",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Place a draft order (Orderer)",
    responses={
        204: {"description": "The order is placed"},
        400: {"model": ErrorResponse, "description": "The order is not a draft"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def place_order(
    order_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    target = _target(order_id)
    try:
        dao.place_order(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/orders/{order_id}/cancellation",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an order that has not been reported (Orderer)",
    responses={
        204: {"description": "The order is cancelled"},
        400: {"model": ErrorResponse, "description": "The order is already reported, closed or cancelled"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def cancel_order(
    order_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    target = _target(order_id)
    try:
        dao.cancel_order(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Milestone 1: the staff READ surface (patients, orders, consents) ───────────────────────────────
#
# READ-ONLY, ORGANISATION-SCOPED, ROLE-GATED INSIDE THE DOMAIN METHOD (the first statement of each `read_*` /
# `list_*` method, so a refusal never depends on what an identifier names). Every route is AUDITED by the domain
# method with no payload. No route accepts an organisation: `Session.org_id` is the only scope.
#
# LIST PARAMETERS ARE DECLARED `Optional[str]` ON PURPOSE. FastAPI validates a typed query parameter BEFORE the
# handler runs and therefore before the role gate, so `limit=abc` would answer a caller with no reading role a 422
# while a permitted caller got data. The domain method validates AFTER its gate (a bad value is a 400 with fixed
# text), keeping the refusal for an unauthorised caller one 403 whatever the query says.
#
# A detail route answers an absent id, a malformed id and an id in ANOTHER organisation with the same 404.


def _iso(value):
    return None if value is None else value.isoformat()


def _text(value):
    return None if value is None else str(value)


class PatientView(BaseModel):
    """A patient as staff see them. `disabled` patients are included in lists by default."""

    patient_id: str
    name: str
    dob: str
    sex: str
    created_at: str
    disabled: bool


class PatientPage(BaseModel):
    items: list[PatientView]
    total: int
    limit: int
    offset: int


class OrderSummary(BaseModel):
    """An order as every permitted role sees it. `ordered_by` and `clinical_indication` are present on every
    order and are None unless the caller is an Orderer, Interpreter or Approver reading ONE order."""

    order_id: str
    patient_id: str
    test_id: str
    required_scope: str
    consent_id: str
    priority: str
    state: str
    placed_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    created_at: str
    ordered_by: Optional[str] = None
    clinical_indication: Optional[str] = None


class OrderPage(BaseModel):
    items: list[OrderSummary]
    total: int
    limit: int
    offset: int


class ConsentView(BaseModel):
    """A consent as staff see them. `status` DISPLAYS the row (withdrawn, superseded or active); it is not a
    determination that processing is permitted. The staff user ids that recorded or withdrew it are not shown."""

    consent_id: str
    patient_id: str
    scope: str
    status: str
    recorded_at: str
    withdrawn_at: Optional[str] = None
    superseded_by: Optional[str] = None


class ConsentPage(BaseModel):
    items: list[ConsentView]
    total: int
    limit: int
    offset: int


_READ_RESPONSES = {
    400: {"model": ErrorResponse, "description": "A list parameter is malformed (checked after the role gate)"},
    401: {"model": ErrorResponse, "description": "Not authenticated"},
    403: {"model": ErrorResponse, "description": "Not permitted"},
}
_DETAIL_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Not authenticated"},
    403: {"model": ErrorResponse, "description": "Not permitted"},
    404: {"model": ErrorResponse, "description": "Not found (absent, malformed or another organisation's)"},
}


def _patient_view(row):
    return PatientView(
        patient_id=str(row["patient_id"]),
        name=row["name"],
        dob=_iso(row["dob"]),
        sex=row["sex"],
        created_at=_iso(row["created_at"]),
        disabled=bool(row["disabled"]),
    )


def _order_view(row):
    return OrderSummary(
        order_id=str(row["order_id"]),
        patient_id=str(row["patient_id"]),
        test_id=str(row["test_id"]),
        required_scope=row["required_scope"],
        consent_id=str(row["consent_id"]),
        priority=row["priority"],
        state=row["state"],
        placed_at=_iso(row["placed_at"]),
        cancelled_at=_iso(row["cancelled_at"]),
        created_at=_iso(row["created_at"]),
        ordered_by=_text(row["ordered_by"]),
        clinical_indication=row["clinical_indication"],
    )


def _consent_view(row):
    return ConsentView(
        consent_id=str(row["consent_id"]),
        patient_id=str(row["patient_id"]),
        scope=row["scope"],
        status=row["status"],
        recorded_at=_iso(row["recorded_at"]),
        withdrawn_at=_iso(row["withdrawn_at"]),
        superseded_by=_text(row["superseded_by"]),
    )


@router.get(
    "/patients",
    response_model=PatientPage,
    summary="List and search the caller's own organisation's patients (Orderer, Interpreter, Approver)",
    responses=_READ_RESPONSES,
)
async def list_patients(
    q: Optional[str] = None,
    sex: Optional[str] = None,
    disabled: Optional[str] = None,
    limit: Optional[str] = None,
    offset: Optional[str] = None,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> PatientPage:
    """
    `DataAccess.list_patients`, and nothing else. `q` is a case-insensitive contains match on the NAME only
    (2 to 100 characters; `%` and `_` are literal). `disabled=true|false` filters; the default includes both.
    `limit` is a whole number 1 to 200 (default 50); `offset` is 0 or more. Newest first.
    """
    try:
        page = dao.list_patients(session, q=q, sex=sex, disabled=disabled, limit=limit, offset=offset)
    except AuthorizationError:
        raise _forbidden()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return PatientPage(
        items=[_patient_view(r) for r in page["items"]], total=page["total"], limit=page["limit"], offset=page["offset"]
    )


@router.get(
    "/patients/{patient_id}",
    response_model=PatientView,
    summary="One patient of the caller's own organisation (Orderer, Interpreter, Approver)",
    responses=_DETAIL_RESPONSES,
)
async def get_patient(
    patient_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> PatientView:
    """`DataAccess.read_patient`, and nothing else."""
    target = _target(patient_id)
    try:
        row = dao.read_patient(session, target)
    except AuthorizationError:
        raise _forbidden()
    if row is None:
        raise _not_found()
    return _patient_view(row)


@router.get(
    "/orders",
    response_model=OrderPage,
    summary="List and filter the caller's own organisation's orders (Orderer, Lab technician, Interpreter, Approver, Auditor)",
    responses=_READ_RESPONSES,
)
async def list_orders(
    patient_id: Optional[str] = None,
    state: Optional[str] = None,
    test_id: Optional[str] = None,
    priority: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    limit: Optional[str] = None,
    offset: Optional[str] = None,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> OrderPage:
    """
    `DataAccess.list_orders`, and nothing else. A list never carries `clinical_indication` or `ordered_by`.
    `created_from` / `created_to` take an ISO-8601 date or timestamp; a bare `created_to` date means through
    that whole day. A malformed id filter names nothing and returns an empty page.
    """
    try:
        page = dao.list_orders(
            session,
            patient_id=patient_id,
            state=state,
            test_id=test_id,
            priority=priority,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
    except AuthorizationError:
        raise _forbidden()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return OrderPage(
        items=[_order_view(r) for r in page["items"]], total=page["total"], limit=page["limit"], offset=page["offset"]
    )


@router.get(
    "/orders/{order_id}",
    response_model=OrderSummary,
    summary="One order of the caller's own organisation (Orderer, Lab technician, Interpreter, Approver, Auditor)",
    responses=_DETAIL_RESPONSES,
)
async def get_order(
    order_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> OrderSummary:
    """`DataAccess.read_order`, and nothing else. `ordered_by` and `clinical_indication` are shown to Orderer,
    Interpreter and Approver only; to a Lab technician or Auditor they are None."""
    target = _target(order_id)
    try:
        row = dao.read_order(session, target)
    except AuthorizationError:
        raise _forbidden()
    if row is None:
        raise _not_found()
    return _order_view(row)


@router.get(
    "/patients/{patient_id}/consents",
    response_model=ConsentPage,
    summary="List ONE patient's consents in the caller's own organisation (Orderer, Interpreter, Approver, Auditor)",
    responses={**_READ_RESPONSES, 404: {"model": ErrorResponse, "description": "Not found"}},
)
async def list_patient_consents(
    patient_id: str,
    scope: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: Optional[str] = None,
    offset: Optional[str] = None,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ConsentPage:
    """
    `DataAccess.list_patient_consents`, and nothing else. There is NO organisation-wide consent list. `status`
    is `active`, `withdrawn` or `superseded` (a display of the row, not a processing decision). The staff user
    ids that recorded or withdrew a consent are never shown.
    """
    target = _target(patient_id)
    try:
        page = dao.list_patient_consents(session, target, scope=scope, status=status_filter, limit=limit, offset=offset)
    except AuthorizationError:
        raise _forbidden()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if page is None:
        raise _not_found()
    return ConsentPage(
        items=[_consent_view(r) for r in page["items"]], total=page["total"], limit=page["limit"], offset=page["offset"]
    )


@router.get(
    "/consents/{consent_id}",
    response_model=ConsentView,
    summary="One consent of the caller's own organisation (Orderer, Interpreter, Approver, Auditor)",
    responses=_DETAIL_RESPONSES,
)
async def get_consent(
    consent_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ConsentView:
    """`DataAccess.read_consent`, and nothing else."""
    target = _target(consent_id)
    try:
        row = dao.read_consent(session, target)
    except AuthorizationError:
        raise _forbidden()
    if row is None:
        raise _not_found()
    return _consent_view(row)
