"""
clinical/admin_endpoints.py
───────────────────────────
w152 (WAVE 2) — THE ADMINISTRATIVE HTTP SURFACE. The outermost layer, which
is the one that was missing.

WHY THIS FILE EXISTS, STATED AS THE MEASUREMENT AND NOT AS A STORY.
`w150-reachability-audit.md` counted fourteen unreachable capabilities and
found that in thirteen of them the broken link is the SAME one and it is the
outermost: no HTTP route and no CLI command. Not one of them is "a route
exists but nothing calls the domain method". Re-measured at `e1be7ab` by AST
(`ast.Call` nodes in production files, never grep — grep credits a docstring
as a caller, which is how `_release_report` came to be recorded as having
thirteen callers and zero calls): `assign_role`, `set_user_disabled`,
`set_patient_login_disabled` and `read_audit` each have ZERO production call
sites, and the seventeen production routes contain ZERO administrative ones.

So this is ONE MISSING SURFACE, not fourteen wiring problems, and this module
is that surface.

WHAT IS AND IS NOT IN IT.
Every handler below is a TRANSLATION — HTTP in, one existing domain method,
HTTP out. There is no business logic here, no second permission system, no
second session type, no second refusal vocabulary and no SQL. The role gate
is `DataAccess._require_role`, called from inside the domain method's own
body, exactly where it already was; this file never asks what roles a caller
holds and never decides. A handler that made that decision itself would be a
second authorisation system whose answer could drift from the first one's,
and the first one is the one the database's composite foreign keys back up.

Four capabilities were wired at w152, a fifth (role revocation) at w154, and
a sixth (patient credential issuance) at w155. The rest are NOT, each for a
stated reason, and the reasons are in `WHAT IS DELIBERATELY NOT HERE` at the
bottom of this docstring. A capability that needs a product decision is
stopped and carded rather than guessed.

w155 (WAVE 5) — TWO SHAPES OF ROUTE, AND WHICH ONE THE NEW ONE IS.
The requirement "a patient ID never comes from an unauthenticated public
request as authority" is met in this repository by TWO different mechanisms
and it matters which is which:

  DEFENCE BY ABSENT PARAMETER — the portal. `list_my_report_trend(self,
  patient_session)` takes NO patient identifier, so the wrong version is
  UNWRITABLE rather than merely untested. Every `/portal/` route is this
  shape and none of them may ever stop being it.

  AN AUTHENTICATED ADMINISTRATOR NAMING A TARGET — this surface, including
  `POST /admin/patients/{patient_id}/logins`. Administration is by definition
  acting on somebody else, so the identifier is in the path. It is not an
  authority claim and cannot become one: the caller is authenticated as
  STAFF, the ORGANISATION the identifier is resolved in comes from
  `Session.org_id` and never from the request, and the role gate runs before
  the identifier is read at all. A `patient_id` here selects a row WITHIN the
  caller's own tenant; it never selects an identity, and it never selects a
  tenant.

The one shape that exists nowhere is the third: an UNAUTHENTICATED request
carrying a patient identifier. There is no self-registration route, and
`test_w155_patient_administration.py` proves it by enumerating the served
surface rather than by asserting it.

──────────────────────────────────────────────────────────────────────────
THE SECURITY MODEL, IN THE ORDER A REQUEST MEETS IT
──────────────────────────────────────────────────────────────────────────

1. AUTHENTICATION — `Depends(_get_session)`, imported from
   `clinical/endpoints.py` and NOT reimplemented. It is the same dependency
   the exception routes use: `_presented_session_id` reads the cookie or the
   bearer header and shape-checks it while touching no database, then
   `DataAccess.session_valid` validates it. Every failure — absent, malformed,
   unknown, expired, terminated, or a PATIENT session id presented to a staff
   route — is `_anonymous_refusal()`: 401, `GENERIC_AUTH_ERROR`, byte for
   byte. There is no route on this router without that dependency, and
   `test_w152_admin_surface.py` proves that by enumerating the router rather
   than by reading it.

2. ORGANISATION CONTEXT — DERIVED, NEVER SUPPLIED. `Session.org_id` comes
   out of the `sessions` row. Not one route below accepts an organisation in
   a path segment, a query parameter or a body field, and the request models
   set `extra="forbid"` so that a body which tries to carry one is REFUSED
   rather than silently ignored — a silent ignore is correct today and is one
   careless `**body` away from not being.

   THE HOUSE PATTERN IS DEFENCE BY ABSENT PARAMETER and it is preferred
   wherever it is expressible: `list_my_report_trend(self, patient_session)`
   takes no patient identifier, so the wrong version is UNWRITABLE. An
   administrative route cannot be quite that shape — administration is by
   definition acting on somebody else — so the weaker form is used and it is
   the same rule one level down: the caller may name a TARGET, and the
   ORGANISATION that target is looked up in comes from the principal. Every
   domain method below binds `org_id = session.org_id` in its own SQL, and
   the schema's composite foreign keys refuse the row even if that layer were
   wrong.

3. ROLE AUTHORISATION — `_require_role`, inside the domain method, FIRST.
   Administrator for the three writes, Auditor for the audit read. The order
   is load-bearing and it is the domain layer's, not this file's:
   `assign_role`'s own docstring says "THE ORDER MATTERS: the Administrator
   check stays FIRST, so a caller without it learns nothing about which roles
   are assignable". Because the gate precedes every target read, a 403 from
   this surface is a function of the CALLER'S ROLES ALONE and carries no
   information whatever about whether the named target exists.

4. AUDIT ATTRIBUTION — `@auditable` on the domain method, unchanged and
   untouched. The actor is `session.user_id`, the organisation is
   `session.org_id`, the target is the `resource_id_param` the decorator
   already names. Nothing in this file writes an audit row.

   AND NOTHING IN THIS FILE COMPOSES TWO DECORATED METHODS INTO ONE
   OPERATION. `@auditable` COMMITS after each audit row it writes, so a
   handler assembled out of "check it, then do it" would commit the first
   half's audit row when the second half failed — an audit that records
   something that did not happen. Each handler calls EXACTLY ONE decorated
   domain method, which is why, for example, the disable routes do not
   read the user back to return it.

5. ERROR MODEL — THREE REFUSALS, EACH A SET OF SIZE ONE ACROSS THE WHOLE
   SURFACE. The precedent is `clinical/portal_endpoints.py`, whose refusals
   are one object, and `clinical/endpoints.py::_anonymous_refusal`, whose
   docstring explains why a per-case equality is not enough (Standing Rule 8:
   an oracle can live in the STATUS channel or the BODY channel, so both are
   pinned, as a set).

       401 + GENERIC_AUTH_ERROR   not authenticated, in any of its six ways
       403 + ADMIN_FORBIDDEN      authenticated, lacks the required role
       404 + ADMIN_NOT_FOUND      no such target IN THIS ORGANISATION

   `ADMIN_FORBIDDEN` and `ADMIN_NOT_FOUND` are module constants and are the
   same string on every route, so a caller cannot tell from a refusal WHICH
   administrative object it named, nor which role it was short of.

   THE SURFACE ALSO ANSWERS 400, 409 AND 422, AND NONE OF THE THREE IS AN
   IDENTIFIER CHANNEL. 400 is the role vocabulary (a closed public list of
   seven strings), raised after the gate. 422 is a request SHAPE refusal
   (a forbidden extra field, a missing one, a `limit` out of range) and is
   the one refusal FastAPI answers BEFORE the handler runs — which is why no
   route on this surface may ever put a VALUE check in a request model.
   Hazard, measured this run: validation order leaks. An `EmailStr` or a
   `min_length` on the enrolment body would be answered to a caller holding
   no Administrator role at all, and the refusal would then vary with
   something evaluated before the caller's authorisation was. The enrolment
   model therefore declares `str` and `str` and checks neither.
   409 is `PatientAlreadyEnrolledError`, raised strictly after the
   Administrator gate AND after the patient-exists-in-this-organisation
   read, so it is visible only to an Administrator naming a patient in their
   own tenant. It varies with nothing outside that tenant.

6. NO INFORMATION ORACLE. The 404 is the same object for an identifier that
   is malformed, an identifier that exists nowhere, and an identifier that
   names a REAL ROW IN ANOTHER ORGANISATION. That last case is the one a
   behavioural test cannot reach by accident — two randomly generated UUIDs
   never collide — so `test_w152_admin_surface.py` CONSTRUCTS the collision:
   it plants a real user, and a real patient credential, in organisation B
   and names them from an organisation A session. Without that row a missing
   `org_id` predicate is invisible to every other test in the file.

   THE 403/404 DISTINCTION IS NOT AN ORACLE, and this is the reasoning rather
   than an assertion. A distinction leaks only if it varies with something the
   caller is not entitled to know. Here 403 depends on the caller's own roles
   and NOTHING else — the gate runs before any target is read, so a
   non-Administrator gets the identical 403 whether the target is real,
   invented, or in another tenant. And 404 depends on existence-within-the-
   caller's-own-organisation and nothing else. Neither varies with the
   existence of another tenant's row. Collapsing 403 into 404 as well would
   cost an Administrator any way to tell "I am not an Administrator" from "no
   such user" and buy nothing.

──────────────────────────────────────────────────────────────────────────
WHAT THIS SURFACE DOES NOT CLAIM — READ THIS BEFORE BELIEVING ANY OF IT
──────────────────────────────────────────────────────────────────────────

NO ROUTE IN THIS FILE IS REACHABLE BY A REAL DEPLOYMENT TODAY. Every one of
them requires a session holding Administrator or Auditor, and `assign_role`
is the only production writer of a human role assignment while itself
requiring an existing Administrator. A fresh deployment has none: measured at
`e1be7ab`, `INSERT INTO role_assignments` occurs at exactly two production
sites, one inside `assign_role` and one that writes `'System'`, which w141
made unassignable to people. That is the first-administrator deadlock and it
is WAVE 3's, not this wave's.

So these routes are IMPLEMENTED + UNREACHABLE, in the report's own
vocabulary, and they are classified that way rather than as complete. The
tests provision their Administrator BY INSERTING THE ROLE ROW DIRECTLY, the
same way `test_identity.py::admin_session` and
`test_w133_staff_revocation.py::_staff_session` already do — a FIXTURE, not
a bootstrap. No bootstrap was invented here to make this file's own tests
pass; inventing one would be doing Wave 3's work badly and would have to be a
permanent part of the product.

──────────────────────────────────────────────────────────────────────────
WHAT IS DELIBERATELY NOT HERE, AND WHY — each of these is a card, not an
oversight. See `hive/agents/god/w152-decision-cards.md`.
──────────────────────────────────────────────────────────────────────────

* ORGANISATION CREATION (`create_organisation`) and STAFF CREATION
  (`create_user`). Both are `@auditable(requires_session=False)` and neither
  has a `_require_role` call anywhere in its body — AST-confirmed, not read
  off the docstring. A route onto either would therefore be an UNAUTHENTICATED
  route that creates a tenant or a principal, or it would need an
  authorisation rule invented at the HTTP layer, which is the second
  permission system this wave is forbidden to build. Which principal may
  create an organisation, and may create a user, is a product decision nobody
  has made. STOPPED.

* FIRST ADMINISTRATOR PROVISIONING. The deadlock above. WAVE 3.

* ROLE REVOCATION. ABSENT AT `e1be7ab`, WHERE THIS BULLET WAS WRITTEN, AND
  BUILT AT w154 (WAVE 4). The measurement that made it absent was right: no
  `DataAccess` method had `revoke` in its name and `revoked_at`/`revoked_by`
  appeared only in `revoked_at IS NULL` read predicates, so the enforcement
  model was continuous and strong and the state it enforced on had no writer.
  `DataAccess.revoke_role` is now that writer and `DELETE
  /admin/users/{user_id}/roles/{role}` is its route. It ends no sessions, and
  that is a measurement rather than an omission -- see the method's own
  docstring, and `revoke_role` below.

* PATIENT CREDENTIAL ISSUANCE (`enrol_patient_login`). STOPPED AT w152 AND
  WIRED AT w155 (WAVE 5). The w152 reasoning is left standing below in full,
  because it was right when it was written and because the thing that changed
  is not the code — it is the authority. READ BOTH HALVES.

  w152's ground, unedited: "Administrator-gated and audited, so it is
  mechanically pure wiring — and it is stopped anyway, on the one ground this
  wave is not allowed to resolve. `GEPER_CLINICAL_PLATFORM_SPEC.md` §16 reads
  '**Recommendation: no patient access in v1**', and carries an unanswered
  EXPERT call-out about what counselling provision must accompany patient
  access. Seven `/portal/` routes are nevertheless mounted and tested, and the
  tension is reported and unresolved. Issuing the credential is the act that
  converts that recommendation into a live patient-facing deployment — nothing
  else can, because there is no self-registration path of any kind. That is a
  product decision, and this wave does not silently reinterpret the product
  spec."

  WHAT SUPERSEDED IT. The human's Wave 5 dispatch instructs, in terms, "Wire
  patient enrolment and credential operations through the administrative
  surface", and sets eight requirements for it, the first being
  Administrator-only enrolment. A product decision that has been MADE is not
  the same object as one that is missing, and the thing w152 correctly refused
  to do — settle §16 BY IMPLEMENTATION — is not what w155 did: the ruling came
  first and from the person entitled to make it. `POST
  /admin/patients/{patient_id}/logins` is that wiring.

  AND THE TENSION IS RECORDED RATHER THAN DISSOLVED, WHICH IS THE PART A
  LATER READER NEEDS. §16 still says what it says. The EXPERT call-out about
  counselling provision is still unanswered. Seven `/portal/` routes and now
  one issuing route are mounted against a spec section that recommends against
  the whole surface, and the correct resolution is an edit to §16 by whoever
  owns it — not a further re-reading by an implementer. It is not reinterpreted
  here, in either direction. See `hive/agents/god/w155-decision-cards.md`.

  REVOCATION WAS WIRED BEFORE ISSUANCE WAS, and the asymmetry was the point
  rather than an inconsistency: revocation only ever CONTRACTS patient access
  and no citation anywhere opposes contracting it, while issuance expands it
  and §16 speaks directly to that. The safe half of a carded pair was not held
  hostage by the half that needed the ruling, and when the ruling came only
  the second half had to move.

* PATIENT RECORD CREATION AND CONSENT (`create_patient`, `record_consent`,
  `withdraw_consent`). STILL STOPPED AT w155, AND THE MEASUREMENT IS WHY.

  Re-measured at 60c48d9 by syntax tree, not grep: all three take a `Session`
  as their first parameter and bind `org_id` from it, all three are
  `@auditable(requires_session=True)`, and NONE of the three contains a
  `_require_role` call anywhere in its body. Production call sites: ZERO for
  each. Test call sites: 86, 57 and 6, across 43 files.

  So the gate CAN be added without breaking a single production caller —
  there are none. The question the gate cannot answer for itself is WHICH
  ROLE, and that is a product decision in the strict sense. "Administrator-only
  enrolment" is a ruling about ENROLMENT, and in this repository's own
  vocabulary enrolment is `enrol_patient_login` — the method named for it,
  whose audit action is `patient_login_enrolled`. `create_patient` is the
  clinical patient REGISTRATION that precedes an order, and `record_consent`
  is consent capture; making either Administrator-only would mean an
  Administrator must register every patient and capture every consent before
  any clinician can order a test. That is a workflow ruling with a cost, it is
  not what the dispatch said, and inventing it here would be exactly the
  "unresolved decision turned into an implementation assumption" the queue
  forbids. STOPPED and carded; no route is added onto any of the three, and
  none is gated, because a gate whose role is guessed is worse than a method
  with no HTTP surface.

  THE CONSEQUENCE IS HONEST AND IT IS STATED: the route this wave DOES add
  names a `patient_id`, and no route on this surface can create one. An
  Administrator enrolling a patient must be given a patient record that some
  other path made. That is the same class of gap as `provision_organisation`
  having no production caller outside `data_access.py` — the capability is
  IMPLEMENTED and the surface above it is not complete.

* PATIENT MFA. BLOCKED — a new security decision is required and none has been
  made. Re-verified at 60c48d9: `patient_logins` has no TOTP column of any
  kind and no patient code path mentions a second factor, so patient MFA is
  ABSENT rather than broken. Staff MFA is present and FAIL-CLOSED-BROKEN:
  `_verify_second_factor` raises `ConfigurationError` when `self._cipher` or
  `self._totp` is `None`, `enrol_totp` raises it too, and every production
  construction of `DataAccess` — `clinical/endpoints.py:262`,
  `geper/api/exception_retry_worker.py:132`, `geper/api/submission_worker.py:850`,
  `geper/review/signoff.py:936` — passes the connection alone, so both are
  always `None` in production. `enrol_totp` also takes the SECRET AS A
  PARAMETER, which means the caller generates it. Completing this needs a
  key-management ruling (where the cipher key lives and who rotates it), a
  secret-generation ruling, and for patients a schema change. No cryptography
  was invented here. See the card.

* REPORT RETRACTION. RESOLVED AT w156 (WAVE 6), and the claim above is
  corrected rather than left standing. `retract_report` did not exist at
  7bb6f8c — the `0003` schema landed and the domain method did not — so it was
  WRITTEN, in this tree's idiom rather than lifted from the held branch
  `w122-retraction` (read, never merged; the reasoning is in the long note
  above the method in `clinical/data_access.py`). It is reachable at
  `POST /admin/reports/{report_id}/retraction`, APPROVER-gated, which makes it
  the second route here whose role is not Administrator.

* EXCEPTION AMENDMENT. STILL BLOCKED, re-measured at 7bb6f8c: `create_amendment`
  (report amendment) exists and is Approver-gated with zero production callers;
  the exception-AMENDMENT verb does not exist, and cannot be written here,
  because `exception_events.action` is CHECKed to exactly
  ('open', 'resolve', 'reopen') and an amendment needs a value the vocabulary
  does not contain. Widening an inline CHECK is DROP + ADD — a destructive
  statement — so it is a MIGRATION, which migration `0004`'s own header says is
  exactly where the held branch stops being additive. w156 was instructed to
  add no schema and STOPPED rather than guessing. The relationship itself is
  landed and proved (`test_w149_migration_0004_amendment.py`): same-org
  accepted, cross-org refused by the database with SQLSTATE 23503, and
  `amends_event_id` still has zero writers anywhere.
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict

from clinical.data_access import (
    AuthorizationError,
    DataAccess,
    NotFoundError,
    PatientAlreadyEnrolledError,
    Session,
)
from clinical.endpoints import _get_data_access, _get_session

# PREFIXED, so that every administrative path is visibly administrative in
# the route census and in any access log, and so that a reverse proxy or a
# future network policy can speak about the whole surface in one rule.
router = APIRouter(prefix="/admin", tags=["administration"])


# ─── THE TWO REFUSALS THIS FILE OWNS ─────────────────────────────────────────
#
# The third one, 401, is `clinical.endpoints._anonymous_refusal` and is not
# re-declared here; this surface authenticates with the staff dependency and
# must not grow a second answer to "who are you".
#
# ONE STRING EACH, FOR EVERY ROUTE AND EVERY TARGET TYPE. "User not found"
# next to "Credential not found" would tell a caller which kind of object its
# guessed identifier named, which is the same class of disclosure as telling
# it the object exists.

ADMIN_FORBIDDEN = "Not permitted."
ADMIN_NOT_FOUND = "Not found."


def _forbidden() -> HTTPException:
    """403. The caller is authenticated and does not hold the required role.

    Carries no information about the target: the role gate runs FIRST, inside
    the domain method, before any row is read.
    """
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ADMIN_FORBIDDEN)


def _not_found() -> HTTPException:
    """404. THE SAME OBJECT for a malformed identifier, an identifier that
    exists nowhere, and an identifier naming a real row in ANOTHER
    organisation. The third case is the one that matters and the one a test
    has to construct on purpose."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ADMIN_NOT_FOUND)


def _target(raw: str) -> uuid.UUID:
    """
    A path identifier, parsed — and a MALFORMED one becomes an identifier
    that names nothing, rather than a refusal of its own.

    DECLARED `str` AND PARSED HERE, NOT DECLARED `uuid.UUID`, and the portal
    made the same choice for the same reason: FastAPI's own path coercion
    answers a malformed id with a 422 whose body names the parameter and the
    error, which makes "not a UUID" distinguishable from "a UUID that names
    nothing". That is a second refusal shape on a surface whose whole claim is
    that it has one.

    AND IT DOES NOT RAISE, WHICH IS THE PART THAT IS EASY TO GET WRONG — the
    first version of this function raised the 404 directly, and that was a
    measurable defect rather than a stylistic one. `_target` runs in the
    HANDLER BODY, which is AFTER the dependencies and BEFORE the domain
    method, so a raising version answered a caller who holds no Administrator
    role with 404 for a malformed id and 403 for a well-formed one. The
    refusal would then have varied with the SHAPE of the identifier at a point
    where the caller's authorisation had not yet been evaluated — the 403 set
    would no longer have been a function of the caller's roles alone, which is
    the whole argument for why a 403 and a 404 may differ here at all.

    Substituting a fresh random identifier makes the malformed case take the
    SAME path as "an id that exists nowhere": the role gate runs first, and
    only an authorised caller reaches the lookup that answers 404. A `uuid4`
    rather than a fixed sentinel because a constant is a value somebody could
    one day insert; a random 122-bit value colliding with a real row is not a
    risk anyone needs to reason about.
    """
    try:
        return uuid.UUID(raw)
    except ValueError:
        return uuid.uuid4()


# ─── request and response models ─────────────────────────────────────────────
#
# `extra="forbid"` ON EVERY REQUEST MODEL. Pydantic's default is to ignore an
# unknown field, which is safe and silent; forbidding it is safe and LOUD. The
# field nobody must ever be able to send is an organisation, and a caller who
# tries gets a 422 rather than a success that quietly did the right thing for
# a reason no test would notice if it stopped being true.


class RoleAssignmentRequest(BaseModel):
    """
    THERE IS NO `org_id` FIELD AND NO `assigned_by` FIELD, and both absences
    are the design. The organisation comes from the session; the assigning
    actor is `session.user_id`, written by `assign_role` itself. A validated
    caller-supplied actor is still a claim, and `ResolutionRequest` in
    `clinical/endpoints.py` already settled that rule for this repository:
    the field is removed rather than checked.
    """

    model_config = ConfigDict(extra="forbid")

    role: str
    # Mandatory for Approver — ISO 15189 7.4.1.5 c) requires the reviewer's
    # authority to be retrievable, and both `assign_role` and the schema CHECK
    # refuse the row without it. NOT re-validated here: that rule lives in the
    # domain method and a copy of it here is a second answer to maintain.
    basis: Optional[str] = None


class DisabledRequest(BaseModel):
    """
    ONE ROUTE FOR BOTH DIRECTIONS, mirroring the one domain method that takes
    a boolean. Two routes (`/disable` and `/enable`) would be two handlers
    around one method and would invite the pair to drift; the audit action is
    already a lambda on this same boolean (`user_disabled` / `user_enabled`),
    so the record distinguishes them without the URL having to.
    """

    model_config = ConfigDict(extra="forbid")

    disabled: bool


class PatientLoginEnrolmentRequest(BaseModel):
    """
    w155 (WAVE 5). The body of `POST /admin/patients/{patient_id}/logins`.

    THERE IS NO `patient_id` FIELD, NO `org_id` FIELD AND NO `enrolled_by`
    FIELD, and all three absences are the design. The patient is the PATH
    TARGET, resolved inside the caller's own organisation; the organisation
    is `Session.org_id`; the issuer is `session.user_id`, written to
    `patient_logins.enrolled_by` by `enrol_patient_login` itself. A
    caller-supplied issuer would be a claim, and the rule this repository
    already settled (`ResolutionRequest` in `clinical/endpoints.py`) is that
    the field is REMOVED rather than validated.

    NEITHER FIELD IS VALIDATED HERE, AND THAT IS DELIBERATE AND
    LOAD-BEARING, not laziness. Pydantic runs BEFORE the handler and
    therefore before the role gate inside the domain method. An `EmailStr`
    or a password `min_length` would be a 422 answered to a caller holding
    no Administrator role, so the refusal would vary with the CONTENT of the
    body at a point where the caller's authorisation had not been evaluated
    — the same defect `_target` was rewritten not to have for the SHAPE of
    an identifier. The gate goes first; everything that could answer before
    it is a leak. `str` and `str`, and no constraint on either.

    THE PASSWORD IS SUPPLIED BY THE ENROLLING ADMINISTRATOR rather than
    generated here, because that is what `enrol_patient_login` already
    takes. Generating one server-side and returning it once would be a new
    decision about a delivery channel nobody has ruled on, and would also
    mean this handler invented a credential policy the domain layer does
    not have. It is never echoed back: `PatientLoginEnrolled` carries the
    identifier and nothing else.
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str


class PatientLoginEnrolled(BaseModel):
    """The credential's identifier, which is also the handle the revocation
    route already takes (`POST /admin/patient-logins/{id}/disabled`). No
    email, no patient name, no password — nothing the caller did not send or
    already hold."""

    patient_login_id: str


class RetractionRequest(BaseModel):
    """
    w156 (WAVE 6). The body of `POST /admin/reports/{report_id}/retraction`.

    THERE IS NO `org_id` FIELD, NO `report_id` FIELD AND NO `retracted_by`
    FIELD, and all three absences are the design, the same as every other
    request model on this surface. The report is the PATH TARGET, resolved
    inside the caller's own organisation; the organisation is
    `Session.org_id`; the retractor is `session.user_id`, written to
    `report_retractions.retracted_by` by `retract_report` itself. A
    caller-supplied retractor would be a claim, and this repository already
    settled that the field is REMOVED rather than validated
    (`ResolutionRequest` in `clinical/endpoints.py`).

    NEITHER FIELD IS VALIDATED HERE, AND THAT IS LOAD-BEARING. Pydantic runs
    BEFORE the handler and therefore before the role gate inside the domain
    method. A `min_length=1` on `reason` would be a 422 answered to a caller
    holding no Approver role, so the refusal would vary with the CONTENT of
    the body at a point where the caller's authorisation had not been
    evaluated -- the same defect `_target` was rewritten not to have for the
    SHAPE of an identifier. The non-empty rule lives in `retract_report`,
    after the gate, and is reached only by an Approver.

    `replacement_report_id` is `Optional[str]` rather than
    `Optional[uuid.UUID]` for exactly that reason: a declared UUID is parsed
    by Pydantic, pre-gate, and a malformed one would answer 422 to a caller
    who should have seen 403. It goes through `_target` in the handler, which
    turns a malformed identifier into one that names nothing.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    # HUMAN RULING E4: optional. NULL means "no replacement report has been
    # issued", which is a statement the schema's nullable column already
    # makes; a required field would force a false row into existence.
    replacement_report_id: Optional[str] = None


class RetractionCreated(BaseModel):
    """
    The retraction's identifier, and nothing else.

    NOT the reason, not the retractor, not the from-state -- not because a
    staff Approver may not see them (they may; they just sent one of them),
    but because a response that re-derived them would be a second view of a
    row the caller can already read, and this surface renders rather than
    interprets.
    """

    retraction_id: str


class RoleAssignmentCreated(BaseModel):
    assignment_id: str


class RoleRevoked(BaseModel):
    """
    HOW MANY LIVE ASSIGNMENTS OF THAT ROLE THIS CALL WITHDREW.

    A RESPONSE model, so it declares no `extra="forbid"` and is deliberately
    outside the inbound scan in `test_w152_admin_surface.py`: there is no
    request body on this route at all.

    The count is here rather than a 204 because zero and one are genuinely
    different outcomes and only one of them changed anything -- revoking a
    role the user never held is not a refusal (the post-condition asked for
    is already true) and a caller is entitled to know which of the two
    happened. It discloses nothing: only an Administrator of the target's own
    organisation ever sees it, and it counts rows in that organisation.
    """

    revoked: int


class AuditEntry(BaseModel):
    """
    The columns `read_audit` selects, PASSED THROUGH AND NOT RE-DERIVED.

    `org_id` is in the shape even though it can only ever be the caller's own
    organisation — the method binds `WHERE org_id = %s` from the session. It
    is kept because it lets a test assert that invariant on the response
    itself rather than on the SQL, and a value that can only be one thing is
    not a disclosure.

    `ip_address` reaches an Auditor because the domain method selects it and
    the Auditor role is the one this platform defines for reading the audit
    trail. That is the existing decision, not a new one; this file discloses
    no column `read_audit` does not return.
    """

    log_id: int
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    action: str
    details: Any = None
    ip_address: Optional[str] = None
    timestamp: str


class ErrorResponse(BaseModel):
    detail: str


# ─── the routes ──────────────────────────────────────────────────────────────


@router.post(
    "/users/{user_id}/roles",
    status_code=status.HTTP_201_CREATED,
    response_model=RoleAssignmentCreated,
    summary="Grant a role to a user in the CALLER'S OWN organisation",
    responses={
        201: {"description": "The role is assigned"},
        400: {"model": ErrorResponse, "description": "Unknown, unassignable, or under-justified role"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def assign_role(
    user_id: str,
    body: RoleAssignmentRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> RoleAssignmentCreated:
    """
    `DataAccess.assign_role`, and nothing else.

    The Administrator check, the `ROLES` vocabulary check, the
    `HUMAN_ASSIGNABLE_ROLES` check that makes `'System'` ungrantable (w141),
    the Approver-needs-a-basis rule and the organisation predicate are all in
    the domain method. NONE of them is repeated here. If this handler
    validated the role name too, there would be two vocabularies and the day
    they disagreed the HTTP one would win silently.

    THE 400 CARRIES THE DOMAIN'S OWN MESSAGE and that is safe: every
    `ValueError` `assign_role` raises is about the ROLE VOCABULARY — a closed,
    public list of seven strings — and is raised strictly AFTER the
    Administrator gate, so only an Administrator of this organisation can see
    one. No identifier, no row and no other tenant appears in any of them.
    """
    target = _target(user_id)
    try:
        assignment_id = dao.assign_role(session, target, body.role, body.basis)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return RoleAssignmentCreated(assignment_id=str(assignment_id))


@router.delete(
    "/users/{user_id}/roles/{role}",
    response_model=RoleRevoked,
    summary="Withdraw a role from a user in the CALLER'S OWN organisation",
    responses={
        200: {"description": "Every live assignment of that role is withdrawn; the count is returned"},
        400: {"model": ErrorResponse, "description": "Unknown or unrevocable role"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def revoke_role(
    user_id: str,
    role: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> RoleRevoked:
    """
    `DataAccess.revoke_role`, and nothing else.

    THE EXACT INVERSE OF THE ROUTE ABOVE, on the same collection. `POST
    /users/{id}/roles` grants; `DELETE /users/{id}/roles/{role}` withdraws.

    THE ROLE IS A PATH SEGMENT AND NOT A BODY FIELD, which is why this route
    adds no request model. A role name is a member of a closed, public
    vocabulary of seven strings, not an identifier -- it names no row, leaks
    nothing by being in a URL, and there is nothing else to send. It is also
    what makes the request idempotent in the way DELETE is supposed to be:
    the second call withdraws nothing and says so.

    NO VOCABULARY CHECK HAPPENS HERE. An unknown role, and "System", are
    both refused by the domain method, after its Administrator gate, with the
    same `ValueError` shape `assign_role` already uses -- so a
    non-Administrator sending a nonsense role still gets the plain 403 and
    learns nothing about the vocabulary. Validating the segment here would
    put a second copy of the role list in this file and would answer before
    the gate.

    NOTHING IS DONE ABOUT THE TARGET'S LIVE SESSIONS, and it is deliberate.
    Roles are resolved per request: `Session` carries no role, `session_valid`
    reads only the `sessions` row, and `_require_role` re-reads
    `role_assignments` on every gated call. The target's next administrative
    request is refused by the gate itself. Ending their sessions would be a
    second mechanism for an effect the first one already has -- and it would
    also log them out of everything they are still entitled to do.
    """
    target = _target(user_id)
    try:
        revoked = dao.revoke_role(session, target, role)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return RoleRevoked(revoked=revoked)


@router.post(
    "/users/{user_id}/disabled",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disable or re-enable a staff user in the CALLER'S OWN organisation",
    responses={
        204: {"description": "The user's disabled flag is set and, on disable, their live sessions end"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def set_user_disabled(
    user_id: str,
    body: DisabledRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    """
    `DataAccess.set_user_disabled`, and nothing else.

    204 AND NO BODY, deliberately. Returning the updated user would mean a
    second decorated call (`get_user`) after the write — and `@auditable`
    commits after each audit row, so a composition is exactly the atomicity
    hazard w133 wrote this method as one body to avoid. An empty response is
    also the honest one: the method returns `None`.

    NO SELF-DISABLE RULE IS ADDED HERE. An Administrator disabling their own
    account ends their own sessions, because the domain method ends the
    target's sessions and the target is them. Whether that should be refused
    is a product decision; the existing rule is that there is no rule, and
    preserving an existing rule includes preserving its absence.
    """
    target = _target(user_id)
    try:
        dao.set_user_disabled(session, target, body.disabled)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class TotpEnrolled(BaseModel):
    """
    THE ONE MOMENT THE SECRET AND THE BACKUP CODES EXIST IN READABLE FORM.

    Neither can be read back: the secret is stored encrypted under the
    deployment's key and is selected by no read method, and the backup codes
    are stored only as bcrypt hashes. An administrator who loses this response
    re-enrols, which is why re-enrolment voids the previous codes.

    `otpauth_uri` IS DELIBERATELY NOT HERE. It would be the convenient thing to
    return -- it is what an authenticator app scans -- but building one means
    choosing an issuer label and an account label, which is what a person sees
    beside a code in their authenticator, and neither has been ruled. The
    secret is what the protocol needs; the label is presentation, and
    presentation is out of scope.

    NO `extra="forbid"`, AND THAT IS NOT AN OMISSION. On this surface that
    setting is the MARKER FOR A REQUEST MODEL -- it is what
    `test_w152_admin_surface.py::test_structurally_no_route_signature_or_
    request_model_names_an_organisation` scans for to decide which models a
    caller can send, and therefore which must never grow an inbound `org_id`.
    Every response model here (`PatientLoginEnrolled`, `RetractionCreated`,
    `RoleAssignmentCreated`, `RoleRevoked`, `AuditEntry`, `ErrorResponse`)
    omits it for the same reason. The first draft of this class carried it,
    and that guard correctly went red: declaring it would have put an OUTBOUND
    model inside an INBOUND scan, where the setting governs nothing and the
    census it feeds would have been wrong about what this surface accepts.
    """

    secret: str
    backup_codes: List[str]


@router.post(
    "/users/{user_id}/totp",
    status_code=status.HTTP_201_CREATED,
    response_model=TotpEnrolled,
    summary="Enrol a second factor for a staff user in the CALLER'S OWN organisation",
    responses={
        201: {"description": "Enrolled; the secret and the backup codes are returned once and never again"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def enrol_totp(
    user_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> TotpEnrolled:
    """
    `DataAccess.enrol_totp`, and nothing else.

    w164. THE OUTERMOST LINK THAT WAS MISSING. `enrol_totp` has been
    Administrator-gated, audited and tenant-bound since Phase 1 and had ZERO
    production callers and no route -- the same shape as thirteen of the
    fourteen capabilities w150 counted. It also could not have worked if it had
    been called: every production `DataAccess` was built without a cipher, so
    the method could only raise. Both halves are fixed in the same commit,
    because either one alone would be a surface over a method that raises, or a
    working method nothing can reach.

    THERE IS NO REQUEST BODY, AND THAT IS THE POINT. The domain method used to
    take the SECRET as a parameter, so the caller chose it. It generates one
    now, and there is no field through which a weaker one could arrive --
    defence by absent parameter, applied to the one value in this system that
    must never be attacker-influenced.

    THE GATE IS NOT HERE. `_require_role(session, "Administrator")` is the
    first executable statement of the domain method, not a dependency on this
    route. Measured at w162: a router-side gate is invisible to the entire HTTP
    surface, and the method is reachable from workers, CLI paths and future
    routers that never pass through this router.
    `clinical/tests/test_w164_mfa.py` carries the row that calls the method
    DIRECTLY with an Orderer session.

    THE ORGANISATION IS NOT HERE EITHER. `user_id` is in the path; the
    organisation comes from `session.org_id`. `get_user` inside the method
    binds `WHERE org_id = %s AND user_id = %s`, so a real user in another
    tenant raises `NotFoundError` and arrives here as the SAME 404 an invented
    identifier gets.

    IT CAN 500, AND THAT IS CORRECT. `ConfigurationError` -- no key configured
    -- is deliberately not converted. The caller here is an authenticated
    Administrator acting inside their own organisation, so there is nobody to
    leak to, and an operator who has not set `CLINICAL_TOTP_SECRET_KEYS` needs
    to learn that rather than be told "not found". The LOGIN route is where
    that exception must be indistinguishable from every other failure, and
    `clinical/auth_endpoints.py::_refuse_uniformly` already flattens it there.
    """
    target = _target(user_id)
    try:
        enrolment = dao.enrol_totp(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    return TotpEnrolled(secret=enrolment.secret, backup_codes=enrolment.backup_codes)


@router.post(
    "/patients/{patient_id}/logins",
    status_code=status.HTTP_201_CREATED,
    response_model=PatientLoginEnrolled,
    summary="Issue a portal credential for a patient in the CALLER'S OWN organisation",
    responses={
        201: {"description": "The credential is issued and the issuer is recorded"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
        409: {"model": ErrorResponse, "description": "This patient already holds a credential"},
    },
)
async def enrol_patient_login(
    patient_id: str,
    body: PatientLoginEnrolmentRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> PatientLoginEnrolled:
    """
    `DataAccess.enrol_patient_login`, and nothing else.

    w155 (WAVE 5). THE ISSUING HALF OF THE PAIR whose revoking half has been
    below since w152. The module docstring carries why the two arrived one
    wave apart and what superseded the stop; this docstring is about the
    route.

    THE SHAPE. An AUTHENTICATED ADMINISTRATOR NAMING A TARGET, which is the
    second of the two shapes the module docstring sets out — not defence by
    absent parameter, and it could not be: enrolment acts on a patient other
    than the caller, so there is a patient identifier and it is in the path.
    What makes that safe is not the identifier's absence but where its
    ORGANISATION comes from. `enrol_patient_login` binds `WHERE patient_id =
    %s AND org_id = %s` with `org_id` from `session.org_id`, so a real
    patient in another tenant reads exactly like an invented one and the
    refusal is byte-identical. The identifier selects a row inside the
    caller's tenant; it never selects a tenant, and it never selects an
    identity.

    THE GATE IS THE DOMAIN METHOD'S AND IT RUNS FIRST. `_require_role(session,
    "Administrator")` is the first statement of `enrol_patient_login`'s body,
    before the patient read and before the uniqueness read. Nothing in this
    handler asks what roles the caller holds. That ordering is what makes the
    403 a function of the caller's own roles alone: a non-Administrator gets
    the identical refusal whether the named patient is real, invented, in
    another tenant, or already enrolled.

    EXACTLY ONE DECORATED CALL. `@auditable` commits after each audit row, so
    a handler that read the patient back, or looked the credential up to
    return its email, would commit the enrolment's audit row and then fail
    with the record already written. The domain method is one `@transactional`
    body under one `@auditable` for that reason and this handler does not
    undo it.

    409, NOT 404 AND NOT 400. `PatientAlreadyEnrolledError` is the domain's
    own refusal and it carries the domain's own message, which explains that
    a second credential for one patient is a PROXY-ACCESS decision rather
    than a re-enrolment. Folding it into the 404 would tell an Administrator
    that a patient they can see does not exist, and folding it into the 403
    would tell them they are not an Administrator. It is raised strictly
    after the gate and after the same-organisation read, so the only
    principal who can ever see it is an Administrator naming a patient in
    their own tenant — it varies with nothing outside it.

    NOTHING IS RETURNED BUT THE IDENTIFIER, and in particular the password is
    not echoed. The credential the caller gets back is the handle the
    revocation route takes.
    """
    target = _target(patient_id)
    try:
        patient_login_id = dao.enrol_patient_login(session, target, body.email, body.password)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except PatientAlreadyEnrolledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return PatientLoginEnrolled(patient_login_id=str(patient_login_id))


@router.post(
    "/patient-logins/{patient_login_id}/disabled",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke or restore a patient portal credential in the CALLER'S OWN organisation",
    responses={
        204: {"description": "The credential's disabled flag is set and, on revoke, its live sessions end"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def set_patient_login_disabled(
    patient_login_id: str,
    body: DisabledRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    """
    `DataAccess.set_patient_login_disabled`, and nothing else.

    THE TARGET IS A CREDENTIAL, NOT A PATIENT. The path segment is a
    `patient_login_id`, which is what the domain method takes and what the
    audit row records, and it is deliberately not a `patient_id`: revocation
    withdraws the thing that mints sessions, and every session that credential
    minted ends with it.

    THIS IS A STAFF ROUTE ON THE STAFF ROUTER, authenticated by the STAFF
    session dependency. It touches patient credentials and no patient
    principal can reach it: a patient session id presented here is refused by
    `_get_session` as the anonymous refusal, like any other non-staff
    credential.

    ISSUANCE IS WIRED AS OF w155, above; between w152 and w155 it was not,
    and the module docstring gives the citation and the asymmetry.

    "REVOCATION INVALIDATES LIVE PATIENT SESSIONS" — MEASURED AT 60c48d9, NOT
    ASSUMED, because the answer decides whether anything had to be built here
    and the wrong answer builds a second mechanism.

    `patient_session_valid` reads ONE `patient_sessions` row by session id and
    checks `terminated_at`, `expires_at` and the idle window. It does NOT join
    `patient_logins` and it does NOT read `disabled`. So patient session
    validation does NOT consult the credential's disabled state, and this is
    the w133 `users.disabled` case rather than the w154 role case: the flag is
    read by nothing on the authenticated path, so writing it invalidates
    nothing by itself, and the termination MUST live in the domain method's
    body. It already does — the `UPDATE patient_sessions SET terminated_at`
    keyed on `patient_login_id`, inside the same `@transactional` body. NO
    SECOND MECHANISM WAS ADDED AT w155, because the first one is the only one
    and adding another could only disagree with it. What w155 added is the
    END-TO-END PROOF the requirement actually asks for: a live patient cookie
    that fetches `/portal/me` before this route is called and is refused by
    the same cookie afterwards, in
    `test_w155_patient_administration.py::TestRevocationInvalidatesLivePatientSessions`.
    """
    target = _target(patient_login_id)
    try:
        dao.set_patient_login_disabled(session, target, body.disabled)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/audit",
    response_model=List[AuditEntry],
    summary="Read the CALLER'S OWN organisation's audit trail (Auditor)",
    responses={
        200: {"description": "Audit entries, oldest first"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
    },
)
async def read_audit(
    action: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> List[AuditEntry]:
    """
    `DataAccess.read_audit`, and nothing else.

    AUDITOR, NOT ADMINISTRATOR — the one route on this surface with a
    different gate, and the gate is the domain method's, unchanged. Reading
    the audit log is itself NOT audited (`@auditable(..., auditable=False)`,
    "audit access is not a resource action"); that is an existing decision and
    this route does not revisit it.

    THERE IS NO ORGANISATION PARAMETER AND NO ACTOR PARAMETER. The only
    filters are `action`, which is a vocabulary rather than an identifier, and
    `limit`. A caller cannot ask for another organisation's trail because
    there is nowhere to put the request, and the method's own
    `WHERE org_id = %s` is bound from the session regardless.

    `limit` IS BOUNDED AT THE EDGE (1..1000) because an unbounded one is an
    availability lever an authenticated Auditor should not have; the domain
    method's default of 100 is unchanged and the bound is a ceiling on the
    parameter, not a change to what the method does with it.

    ROWS ARE RENDERED, NOT INTERPRETED. `details` is passed through as the
    JSONB object the method returned; `ip_address` and the identifiers are
    stringified because a UUID and an `inet` are not JSON. Nothing is
    filtered, reordered or summarised here — a second view of the audit trail
    is a second answer to "what happened".
    """
    try:
        rows = dao.read_audit(session, action or None, limit)
    except AuthorizationError:
        raise _forbidden()

    return [
        AuditEntry(
            log_id=row[0],
            org_id=str(row[1]) if row[1] is not None else None,
            user_id=str(row[2]) if row[2] is not None else None,
            action=row[3],
            details=row[4],
            ip_address=str(row[5]) if row[5] is not None else None,
            timestamp=row[6].isoformat(),
        )
        for row in rows
    ]


@router.post(
    "/reports/{report_id}/retraction",
    status_code=status.HTTP_201_CREATED,
    response_model=RetractionCreated,
    summary="Retract a report in the CALLER'S OWN organisation (Approver)",
    responses={
        201: {"description": "The retraction is recorded"},
        400: {
            "model": ErrorResponse,
            "description": "Not retractable from this state, empty reason, or already retracted",
        },
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found"},
    },
)
async def retract_report(
    report_id: str,
    body: RetractionRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> RetractionCreated:
    """
    `DataAccess.retract_report`, and nothing else.

    APPROVER, NOT ADMINISTRATOR -- the second route on this surface with a
    gate of its own, and the gate is the domain method's, unchanged.
    `/admin/audit` already established that a prefix naming the surface does
    not dictate the role behind it: that one is Auditor. Retraction is an act
    on an approved report, and every verb in this platform that acts on an
    approved report is Approver-gated. The prefix is here so the route is
    visibly administrative in a census and in an access log, which is what
    the prefix is for.

    NO CONDITION IS REPEATED HERE. The Approver check, the E1 state rule, the
    non-empty reason, the E5 already-retracted refusal, the E4 replacement
    rules and every organisation predicate are all in the domain method. If
    this handler re-checked the state there would be two answers to "is this
    retractable", and the day they disagreed the HTTP one would win silently.

    THE 400 CARRIES THE DOMAIN'S OWN MESSAGE, and the check that this is safe
    is the same one `assign_role` made: every `ValueError` `retract_report`
    raises is raised strictly AFTER the Approver gate, so only an Approver of
    THIS organisation can see one. The already-retracted message names the
    `report_id` THE CALLER JUST SENT, and the state message names a state of
    a report in the caller's own organisation. Neither names another tenant,
    another row, or an identifier the caller did not supply.

    THE REPLACEMENT IDENTIFIER GOES THROUGH `_target` for the reason that
    function's docstring gives: a malformed one becomes an identifier that
    names nothing, so it takes the same path as an id that exists nowhere,
    and the role gate still runs first.
    """
    target = _target(report_id)
    replacement = _target(body.replacement_report_id) if body.replacement_report_id is not None else None
    try:
        retraction_id = dao.retract_report(session, target, body.reason, replacement)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return RetractionCreated(retraction_id=str(retraction_id))


# ─── the test catalogue (stage 2) ────────────────────────────────────────────
#
# ADMINISTRATOR-GATED, inside the domain method and first. The catalogue is
# per-organisation; there is no org_id field on any body here because the
# organisation is Session.org_id. A test is created and retired, never edited.


class TestCreateRequest(BaseModel):
    """The body of `POST /admin/tests`. `extra="forbid"`, so a body naming an
    organisation or an approver is refused rather than ignored. Every value
    rule (name length, assembly vocabulary, gene-symbol shape) lives in
    `DataAccess.create_test`, after its role gate."""

    model_config = ConfigDict(extra="forbid")

    name: str
    assembly: str
    genes: List[str]
    cdsco_class: Optional[str] = None


class TestCreated(BaseModel):
    test_id: str


@router.post(
    "/tests",
    status_code=status.HTTP_201_CREATED,
    response_model=TestCreated,
    summary="Add a test to the caller's own organisation's catalogue (Administrator)",
    responses={
        201: {"description": "The test is active and orderable"},
        400: {"model": ErrorResponse, "description": "Invalid name, assembly or gene list; or the name is in use"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
    },
)
async def create_test(
    body: TestCreateRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> TestCreated:
    """`DataAccess.create_test`, and nothing else."""
    try:
        test_id = dao.create_test(session, body.name, body.assembly, body.genes, body.cdsco_class)
    except AuthorizationError:
        raise _forbidden()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return TestCreated(test_id=str(test_id))


@router.post(
    "/tests/{test_id}/retirement",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retire a test so it can no longer be ordered (Administrator)",
    responses={
        204: {"description": "The test is retired"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        403: {"model": ErrorResponse, "description": "Not permitted"},
        404: {"model": ErrorResponse, "description": "Not found, in another organisation, or already retired"},
    },
)
async def retire_test(
    test_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    """`DataAccess.retire_test`, and nothing else. The path id goes through
    `_target` so a malformed one still meets the role gate first."""
    target = _target(test_id)
    try:
        dao.retire_test(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
