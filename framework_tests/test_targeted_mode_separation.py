"""WS4: standard and diagnostic targeted results stay independently addressable
and a diagnostic run can never overwrite, erase, or improve the standard one.
"""

from __future__ import annotations

import json

from calee_regression import targeted_repeat as tr
from calee_regression.models import DEVICE_INIT_SKIP, DEVICE_INIT_STANDARD, certification_block


def _report(device_mode, status, invocation_id):
    report = {
        "invocationId": invocation_id,
        "status": status,
        "startedAt": "t0",
        "finishedAt": "t1",
    }
    report.update(certification_block(device_mode))
    return report


def test_mode_label():
    assert tr.mode_label(DEVICE_INIT_STANDARD) == "standard"
    assert tr.mode_label(DEVICE_INIT_SKIP) == "diagnostic"


def test_standard_then_diagnostic_preserves_both(tmp_path):
    base = tmp_path / "tablet-targeted"
    (base / "standard").mkdir(parents=True)
    (base / "diagnostic").mkdir(parents=True)
    tr.update_targeted_top_index(base, "standard", _report(DEVICE_INIT_STANDARD, "pass", "inv-s1"))
    tr.update_targeted_top_index(base, "diagnostic", _report(DEVICE_INIT_SKIP, "pass", "inv-d1"))

    doc = json.loads((base / "index.json").read_text())
    assert set(doc["modes"]) == {"standard", "diagnostic"}
    assert doc["canonical"]["certifyingMode"] == "standard"
    assert doc["canonical"]["certifyingStatus"] == "pass"
    assert doc["canonical"]["certificationEligible"] is True
    assert doc["modes"]["diagnostic"]["certificationEligible"] is False


def test_diagnostic_then_standard_preserves_both(tmp_path):
    base = tmp_path / "tablet-targeted"
    tr.update_targeted_top_index(base, "diagnostic", _report(DEVICE_INIT_SKIP, "pass", "inv-d1"))
    tr.update_targeted_top_index(base, "standard", _report(DEVICE_INIT_STANDARD, "fail", "inv-s1"))

    doc = json.loads((base / "index.json").read_text())
    assert set(doc["modes"]) == {"standard", "diagnostic"}
    # The certifying result is the standard one even though diagnostic ran first.
    assert doc["canonical"]["certifyingStatus"] == "fail"
    assert doc["canonical"]["hasDiagnostic"] is True


def test_later_pass_cannot_erase_earlier_fail_in_index(tmp_path):
    base = tmp_path / "tablet-targeted"
    tr.update_targeted_top_index(base, "standard", _report(DEVICE_INIT_STANDARD, "fail", "inv-s1"))
    tr.update_targeted_top_index(base, "standard", _report(DEVICE_INIT_STANDARD, "pass", "inv-s2"))

    doc = json.loads((base / "index.json").read_text())
    invocations = doc["modes"]["standard"]["invocations"]
    statuses = {e["invocationId"]: e["status"] for e in invocations}
    # Both invocations are retained -- the earlier fail is never dropped.
    assert statuses == {"inv-s1": "fail", "inv-s2": "pass"}


def test_diagnostic_pass_cannot_improve_standard_certification(tmp_path):
    base = tmp_path / "tablet-targeted"
    tr.update_targeted_top_index(base, "standard", _report(DEVICE_INIT_STANDARD, "fail", "inv-s1"))
    tr.update_targeted_top_index(base, "diagnostic", _report(DEVICE_INIT_SKIP, "pass", "inv-d1"))

    doc = json.loads((base / "index.json").read_text())
    # The diagnostic PASS does not touch the standard certifying result.
    assert doc["canonical"]["certifyingMode"] == "standard"
    assert doc["canonical"]["certifyingStatus"] == "fail"


def test_run_targeted_writes_into_mode_subdirs(tmp_path):
    base = tmp_path / "tablet-targeted"

    def fake_run_once(scenario, attempt_dir):
        return {
            "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
            "scenarios": [{"status": "passed", "mandatory": True}],
        }

    std_dir = base / "standard"
    std_dir.mkdir(parents=True)
    report_s, status_s = tr.run_targeted(
        scenarios=["a.yaml"], repeat_count=1, out_dir=std_dir,
        run_once=fake_run_once, device_initialization_mode=DEVICE_INIT_STANDARD,
    )
    tr.update_targeted_top_index(base, "standard", report_s)

    dia_dir = base / "diagnostic"
    dia_dir.mkdir(parents=True)
    report_d, status_d = tr.run_targeted(
        scenarios=["a.yaml"], repeat_count=1, out_dir=dia_dir,
        run_once=fake_run_once, device_initialization_mode=DEVICE_INIT_SKIP,
    )
    tr.update_targeted_top_index(base, "diagnostic", report_d)

    # Both mode results exist and are separate; neither overwrote the other.
    assert (std_dir / "results.json").exists()
    assert (dia_dir / "results.json").exists()
    assert status_s == "pass" and status_d == "pass"
    assert report_s["certificationEligible"] is True
    assert report_d["certificationEligible"] is False
    doc = json.loads((base / "index.json").read_text())
    assert doc["modes"]["standard"]["certificationEligible"] is True
    assert doc["modes"]["diagnostic"]["certificationEligible"] is False
