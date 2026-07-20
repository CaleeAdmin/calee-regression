"""Priority 6 -- provision the today-relative subscribed fixture and hand its
event titles to the tablet scenario.

The end-to-end contract (see docs/SUBSCRIBED_CALENDAR_REGRESSION.md):

  1. resolve ONE date + timezone for the run;
  2. generate the subscribed ICS for that date (subscribed_fixture);
  3. provision it through an AUTHENTICATED, regression-only mechanism (the
     ``provisioner`` seam -- a real one POSTs to the hub's authenticated
     regression subscription-source endpoint; NEVER an unauthenticated reset,
     never a customer calendar). Provisioning REPLACES any stale feed on the
     dedicated regression source deterministically;
  4. record fixture evidence under the current run;
  5. expose the generated event titles as scenario variables so the tablet
     scenario asserts the exact events THIS run provisioned;
  6/7 (physical) the scenario asserts the same subscribed event on Today and
     Calendar; the source is cleaned up / replaced on the next run.

This module never fabricates a provisioning success: with no authenticated
provisioner available (the usual state in an offline/CI session), it records a
BLOCKED result -- the subscribed scenario then stays draft-unverified/BLOCKED,
never a fake pass.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Callable

from .subscribed_fixture import (
    DEFAULT_TIMEZONE,
    allday_event_name,
    fixture_evidence,
    generate_today_relative_ics,
    resolve_target_date,
    timed_event_name,
)

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"

# The dedicated regression account/source the authenticated endpoint is scoped
# to. The endpoint must refuse any other account (a customer calendar is never
# touched) -- enforced server-side; recorded here for the audit trail.
REGRESSION_ACCOUNT = "regression"
REGRESSION_CALENDAR_ID = "regression:regsub"


@dataclass
class ProvisionResponse:
    """What an authenticated provisioner returns. ``replaced_stale`` records that
    the endpoint deterministically replaced a previous regression feed rather
    than stacking a second one."""

    ok: bool
    replaced_stale: bool = False
    detail: str = ""
    audit_id: "str | None" = None


# A provisioner takes the ICS text and the scoped account/calendar and returns a
# ProvisionResponse. Injected so this is offline-testable; a real one is an
# authenticated HTTPS call to the hub's regression endpoint.
Provisioner = Callable[..., ProvisionResponse]


@dataclass
class SubscribedProvisionResult:
    status: str
    run_id: "str | None" = None
    run_token: "str | None" = None
    resolved_date: "str | None" = None
    timezone: str = DEFAULT_TIMEZONE
    account: str = REGRESSION_ACCOUNT
    calendar_id: str = REGRESSION_CALENDAR_ID
    events: dict = field(default_factory=dict)
    variables: dict = field(default_factory=dict)
    ics: "str | None" = None
    replaced_stale: bool = False
    audit_id: "str | None" = None
    detail: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict:
        # NB: the ICS body is deliberately NOT written to the evidence json (it
        # is provisioning input, not a result); only the identifying facts are.
        return {
            "status": self.status,
            "runId": self.run_id,
            "runToken": self.run_token,
            "resolvedDate": self.resolved_date,
            "timezone": self.timezone,
            "account": self.account,
            "calendarId": self.calendar_id,
            "events": dict(self.events),
            "variables": dict(self.variables),
            "replacedStale": self.replaced_stale,
            "auditId": self.audit_id,
            "detail": list(self.detail),
        }


# The authenticated regression endpoint the real provisioner calls. It is
# APP_ENV-gated off in production and scoped to the dedicated regression account
# server-side (see the calee-hub-core regression provisioning PR).
PROVISION_ENDPOINT_PATH = "/v1/admin/regression/subscribed-source"


def http_provisioner(base_url: str, *, token: str, timeout: float = 30.0, opener=None) -> Provisioner:
    """An authenticated Provisioner that POSTs the ICS to the hub's regression
    subscription-source endpoint over HTTPS with a bearer token. The token
    authenticates the regression operator; the endpoint enforces
    regression-account scoping and production-disablement server-side. The URL
    opener is injectable so this is testable without a live hub."""
    import json
    import urllib.request

    endpoint = base_url.rstrip("/") + PROVISION_ENDPOINT_PATH
    _open = opener or urllib.request.urlopen

    def _provision(ics: str, *, account: str, calendar_id: str) -> ProvisionResponse:
        # `replace: true` -> the endpoint deterministically replaces any stale
        # regression feed instead of stacking a second one.
        body = json.dumps({
            "account": account, "calendarId": calendar_id, "ics": ics, "replace": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with _open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
            parsed = json.loads(raw)
        except Exception as exc:  # network/HTTP/JSON problem -> not ok (BLOCK upstream)
            return ProvisionResponse(ok=False, detail=f"{type(exc).__name__}: {exc}")
        return ProvisionResponse(
            ok=bool(parsed.get("ok")),
            replaced_stale=bool(parsed.get("replacedStale")),
            detail=str(parsed.get("detail", "")),
            audit_id=parsed.get("auditId"),
        )

    return _provision


def build_run_token(run_id: "str | None") -> str:
    """A short, filesystem/title-safe token identifying this run's subscribed
    events. Derived from the run id so it is stable within a run and unique
    across runs; falls back to a fixed token when there is no run id."""
    if not run_id:
        return "LOCAL"
    token = "".join(ch for ch in str(run_id) if ch.isalnum())
    # Keep it short but distinctive (tail carries the random run suffix).
    return (token[-12:] or "LOCAL").upper()


def scenario_variables(target_date: _dt.date, *, run_token: str) -> dict:
    """The run-scoped variables the tablet scenario substitutes for its
    subscribed-event placeholders."""
    return {
        "REG_SUB_TIMED_TITLE": timed_event_name(run_token),
        "REG_SUB_ALLDAY_TITLE": allday_event_name(run_token),
        "REG_SUB_DATE": target_date.isoformat(),
    }


def provision_subscribed_fixture(
    *,
    run_id: "str | None",
    target_date: "_dt.date | None" = None,
    timezone: str = DEFAULT_TIMEZONE,
    account: str = REGRESSION_ACCOUNT,
    calendar_id: str = REGRESSION_CALENDAR_ID,
    provisioner: "Provisioner | None" = None,
) -> SubscribedProvisionResult:
    """Resolve the date once, generate the today-relative ICS, provision it via
    the authenticated ``provisioner``, and record the evidence + scenario
    variables. With no provisioner (offline/CI), returns BLOCKED and never
    fabricates a provisioning success."""
    token = build_run_token(run_id)
    date_ = resolve_target_date(target_date)
    ics = generate_today_relative_ics(date_, run_token=token)
    evidence = fixture_evidence(date_, run_token=token, timezone=timezone)
    variables = scenario_variables(date_, run_token=token)

    result = SubscribedProvisionResult(
        status=STATUS_BLOCKED,
        run_id=run_id,
        run_token=token,
        resolved_date=evidence["resolvedDate"],
        timezone=timezone,
        account=account,
        calendar_id=calendar_id,
        events=evidence["events"],
        variables=variables,
        ics=ics,
    )

    if provisioner is None:
        result.detail.append(
            "No authenticated regression subscription provisioner is configured (no hub backend in "
            "this environment). The today-relative ICS was generated and its evidence recorded, but "
            "the subscribed scenario stays BLOCKED/draft-unverified -- provisioning is never faked and "
            "never done via an unauthenticated reset."
        )
        return result

    try:
        response = provisioner(ics, account=account, calendar_id=calendar_id)
    except Exception as exc:  # a provisioner problem BLOCKS, never a product FAIL
        result.detail.append(f"Authenticated provisioning could not run: {exc}")
        return result

    if not getattr(response, "ok", False):
        result.detail.append(
            f"Authenticated provisioning did not succeed: {getattr(response, 'detail', '') or 'unknown error'}."
        )
        return result

    result.status = STATUS_OK
    result.replaced_stale = bool(getattr(response, "replaced_stale", False))
    result.audit_id = getattr(response, "audit_id", None)
    result.detail.append(
        f"Provisioned the today-relative subscribed feed for {account}/{calendar_id} on "
        f"{result.resolved_date} ({'replaced a stale feed' if result.replaced_stale else 'new feed'})."
    )
    return result
