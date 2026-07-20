"""SUPERSEDED -- exercises calee_regression.subscribed_provision, which is
itself superseded by subscribed_publisher.py (see that module's docstring and
docs/SUBSCRIBED_CALENDAR_REGRESSION.md). Kept only because subscribed_provision.py
is kept as reference; nothing in cli.py wires this path up any longer. The
LIVE Priority 5/6 coverage is framework_tests/test_subscribed_publisher.py and
framework_tests/test_subscribed_fixture_component.py.

Original docstring, describing the superseded contract, follows:

Priority 6 -- the today-relative subscribed fixture is generated, provisioned
through an authenticated regression-only seam, recorded, and its titles reach
the tablet scenario. Offline: the provisioner is injected; no hub backend.

Backend-enforced properties (authenticated access, wrong-account rejection,
production-disabled) are contract-tested in the calee-hub-core provisioning PR;
here we test the client contract and that the client never fabricates a success.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from calee_regression import ics_contract, runner
from calee_regression import subscribed_provision as sp
from calee_regression.subscribed_provision import ProvisionResponse

SUB_SCENARIO = __import__("pathlib").Path(__file__).resolve().parents[1] / "scenarios" / "subscribed_calendar.yaml"


def _ok_provisioner(record):
    def _p(ics, *, account, calendar_id):
        record["ics"] = ics
        record["account"] = account
        record["calendar_id"] = calendar_id
        return ProvisionResponse(ok=True, replaced_stale=True, audit_id="audit-123")
    return _p


# ── generation / evidence contract ─────────────────────────────────────────


def test_resolves_target_date_once_and_records_it():
    rec = {}
    res = sp.provision_subscribed_fixture(
        run_id="release-20260720-101010-abc123", target_date=date(2026, 7, 20),
        provisioner=_ok_provisioner(rec),
    )
    assert res.resolved_date == "2026-07-20"
    # The generated ICS is for that exact date.
    assert "DTSTART:20260720T120000" in rec["ics"]


def test_australia_perth_timezone_is_recorded_in_evidence():
    res = sp.provision_subscribed_fixture(run_id="r-1", target_date=date(2026, 7, 20), provisioner=_ok_provisioner({}))
    assert res.timezone == "Australia/Perth"
    assert res.to_dict()["timezone"] == "Australia/Perth"


def test_all_day_event_date_is_preserved_no_shift():
    rec = {}
    sp.provision_subscribed_fixture(run_id="r-1", target_date=date(2026, 7, 20), provisioner=_ok_provisioner(rec))
    occ = ics_contract.expand(rec["ics"])
    allday = [o for o in occ if o.summary.startswith("REG-SUB-ALLDAY")]
    assert allday and allday[0].all_day is True
    assert allday[0].visible_dates == [date(2026, 7, 20)]  # exact date, no off-by-one


def test_floating_timed_event_is_visible_on_target_date_in_any_timezone():
    rec = {}
    sp.provision_subscribed_fixture(run_id="r-1", target_date=date(2026, 7, 20), provisioner=_ok_provisioner(rec))
    # No trailing 'Z' -> floating local -> visible day == target date regardless of tz.
    assert "T120000Z" not in rec["ics"]
    occ = ics_contract.expand(rec["ics"])
    timed = [o for o in occ if o.summary.startswith("REG-SUB-TIMED")]
    assert timed and timed[0].all_day is False
    assert timed[0].visible_dates == [date(2026, 7, 20)]


def test_titles_are_run_specific():
    a = sp.provision_subscribed_fixture(run_id="release-20260720-101010-aaaaaa", target_date=date(2026, 7, 20), provisioner=_ok_provisioner({}))
    b = sp.provision_subscribed_fixture(run_id="release-20260720-101010-bbbbbb", target_date=date(2026, 7, 20), provisioner=_ok_provisioner({}))
    assert a.variables["REG_SUB_TIMED_TITLE"] != b.variables["REG_SUB_TIMED_TITLE"]
    assert a.run_token != b.run_token


def test_stale_feed_replacement_is_recorded():
    res = sp.provision_subscribed_fixture(run_id="r-1", target_date=date(2026, 7, 20), provisioner=_ok_provisioner({}))
    assert res.ok and res.replaced_stale is True
    assert res.to_dict()["replacedStale"] is True


def test_default_date_is_today():
    res = sp.provision_subscribed_fixture(run_id="r-1", provisioner=_ok_provisioner({}))
    assert res.resolved_date == date.today().isoformat()


# ── provisioning boundary contract ─────────────────────────────────────────


def test_no_provisioner_blocks_and_never_fabricates_success():
    res = sp.provision_subscribed_fixture(run_id="r-1", target_date=date(2026, 7, 20), provisioner=None)
    assert not res.ok and res.status == sp.STATUS_BLOCKED
    # Evidence + variables are still generated (for a later provisioned run).
    assert res.variables["REG_SUB_TIMED_TITLE"].startswith("REG-SUB-TIMED-")
    assert any("never faked" in d for d in res.detail)


def test_backend_rejection_blocks():
    # A backend that rejects (wrong account / production-disabled / auth) -> the
    # client records BLOCKED, never a pass.
    def _reject(ics, *, account, calendar_id):
        return ProvisionResponse(ok=False, detail="regression endpoint disabled in production")
    res = sp.provision_subscribed_fixture(run_id="r-1", target_date=date(2026, 7, 20), provisioner=_reject)
    assert not res.ok
    assert any("did not succeed" in d for d in res.detail)


def test_client_is_always_scoped_to_the_regression_account():
    rec = {}
    sp.provision_subscribed_fixture(run_id="r-1", target_date=date(2026, 7, 20), provisioner=_ok_provisioner(rec))
    # The client never provisions an arbitrary account/calendar -- only the
    # dedicated regression source (server also enforces this).
    assert rec["account"] == sp.REGRESSION_ACCOUNT
    assert rec["calendar_id"] == sp.REGRESSION_CALENDAR_ID


def test_http_provisioner_is_authenticated_and_requests_replacement():
    captured = {}

    class _Resp:
        def __init__(self, body):
            self._body = body.encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode())
        captured["method"] = req.get_method()
        return _Resp(json.dumps({"ok": True, "replacedStale": True, "auditId": "a1"}))

    prov = sp.http_provisioner("https://hub-staging.calee.com.au", token="operator-token-xyz", opener=_opener)
    resp = prov("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", account=sp.REGRESSION_ACCOUNT, calendar_id=sp.REGRESSION_CALENDAR_ID)
    assert resp.ok and resp.replaced_stale
    # Authenticated (bearer), correct endpoint, POST, replace requested, scoped.
    assert captured["method"] == "POST"
    assert captured["url"].endswith(sp.PROVISION_ENDPOINT_PATH)
    auth = {k.lower(): v for k, v in captured["headers"].items()}.get("authorization")
    assert auth == "Bearer operator-token-xyz"
    assert captured["body"]["replace"] is True
    assert captured["body"]["calendarId"] == sp.REGRESSION_CALENDAR_ID
    # The token never appears in the URL.
    assert "operator-token-xyz" not in captured["url"]


# ── tablet scenario variable substitution ──────────────────────────────────


def test_tablet_scenario_substitutes_generated_subscribed_titles():
    res = sp.provision_subscribed_fixture(
        run_id="release-20260720-101010-abc123", target_date=date(2026, 7, 20), provisioner=_ok_provisioner({}),
    )
    scenario = runner.load_scenario(SUB_SCENARIO, variables=res.variables)
    texts = [s.get("text") for s in scenario.steps if s.get("action") in ("assert_text", "wait_for_text")]
    # The placeholder is gone; the run's generated subscribed title is present.
    assert res.variables["REG_SUB_TIMED_TITLE"] in texts
    assert not any("${REG_SUB_TIMED_TITLE}" in (t or "") for t in texts)


def test_scenario_without_variables_leaves_placeholders_verbatim():
    # Parse-contract loads (no variables) keep the templated scenario loadable.
    scenario = runner.load_scenario(SUB_SCENARIO)
    assert any("${REG_SUB_TIMED_TITLE}" in (s.get("text") or "") for s in scenario.steps)


def test_undefined_variable_blocks_the_scenario():
    import pytest
    with pytest.raises(runner.ScenarioError):
        runner.substitute_variables([{"text": "${MISSING_VAR}"}], {"OTHER": "x"})


def test_generator_date_correct_for_future_dates():
    for target in (date(2027, 1, 1), date.today() + timedelta(days=200)):
        rec = {}
        sp.provision_subscribed_fixture(run_id="r-1", target_date=target, provisioner=_ok_provisioner(rec))
        occ = ics_contract.expand(rec["ics"])
        assert all(o.visible_dates == [target] for o in occ)
