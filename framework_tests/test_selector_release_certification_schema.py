"""Priority 8 -- the shared release-certification schema fixture.

schemas/selector_release_certification.schema.json is intentionally
duplicated byte-for-byte in both CaleeAdmin/calee-regression and
CaleeAdmin/CaleeMobile-Regression (see that file's own "$id"/description).
This test loads THIS repository's local copy, validates representative
payloads against its declared required-field set, and locks in the file's
exact content digest -- CaleeMobile-Regression's mirrored contract test
hardcodes the SAME digest, so if either copy drifts from the other without
both being updated together, at least one repository's test fails.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from calee_regression import selector_evidence as se
from calee_regression.suites import REPO_ROOT

SCHEMA_PATH = REPO_ROOT / "schemas" / "selector_release_certification.schema.json"

# Hardcoded content digest of schemas/selector_release_certification.schema.
# json. MUST equal the same constant in CaleeMobile-Regression's mirrored
# test (ui/test_selector_release_certification_schema.py or equivalent) --
# that is the cross-repo half of this contract test.
EXPECTED_SCHEMA_DIGEST = "7a37327fa04b4891564b738f661b1455aa1fe986a543567e2d55ac9b83d7b315"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_file_exists_and_is_valid_json():
    assert SCHEMA_PATH.is_file(), f"missing shared schema fixture: {SCHEMA_PATH}"
    schema = _load_schema()
    assert schema["title"]


def test_schema_content_digest_matches_the_cross_repo_contract():
    digest = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
    assert digest == EXPECTED_SCHEMA_DIGEST, (
        "schemas/selector_release_certification.schema.json changed without updating "
        "the mirrored copy (and this hardcoded digest) in CaleeMobile-Regression -- "
        "update both together."
    )


def test_schema_required_fields_match_priority_8_vocabulary():
    schema = _load_schema()
    required = set(schema["required"])
    assert required == {
        "schemaVersion", "component", "testedSha", "pubspecVersion", "contract", "releaseId", "correlationId",
    }
    # Every required/declared field name also has a documented property entry.
    for field_name in required:
        assert field_name in schema["properties"], f"{field_name} is required but undocumented"
    for optional in ("expectedSha", "expectedVersion", "releaseRunId", "workflowRunId"):
        assert optional in schema["properties"]


def _valid_payload(**overrides) -> dict:
    payload = {
        "schemaVersion": 1,
        "component": "caleemobile-selector-contract",
        "testedSha": "a" * 40,
        "pubspecVersion": "0.0.24+24",
        "contract": "PASS",
        "releaseId": "2026.07.20-rc3",
        "correlationId": "corr-123",
    }
    payload.update(overrides)
    return payload


def _check_against_schema(schema: dict, payload: dict) -> "list[str]":
    """A tiny, dependency-free structural check (this repo has no jsonschema
    dependency -- see pyproject.toml) sufficient to validate the specific
    contract this schema declares: required keys present, const/enum/pattern
    fields obeyed for the fields present."""
    import re

    problems = []
    for key in schema["required"]:
        if key not in payload:
            problems.append(f"missing required field {key!r}")
    for key, spec in schema["properties"].items():
        if key not in payload:
            continue
        value = payload[key]
        if "const" in spec and value != spec["const"]:
            problems.append(f"{key}={value!r} != const {spec['const']!r}")
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{key}={value!r} not in enum {spec['enum']}")
        if "pattern" in spec and value is not None and not re.match(spec["pattern"], str(value)):
            problems.append(f"{key}={value!r} does not match pattern {spec['pattern']!r}")
    return problems


def test_a_real_gate_reported_payload_validates_against_the_schema():
    schema = _load_schema()
    result = se.SelectorContractResult(
        schema_version=1, component="caleemobile-selector-contract",
        tested_sha="a" * 40, pubspec_version="0.0.24+24", contract="PASS",
        release_id="2026.07.20-rc3", correlation_id="corr-123",
        expected_sha="a" * 40, expected_version="0.0.24+24",
    )
    problems = _check_against_schema(schema, result.to_dict())
    assert problems == [], problems


def test_payload_missing_release_id_fails_schema_validation():
    schema = _load_schema()
    payload = _valid_payload()
    del payload["releaseId"]
    problems = _check_against_schema(schema, payload)
    assert any("releaseId" in p for p in problems)


def test_payload_with_wrong_component_fails_schema_validation():
    schema = _load_schema()
    problems = _check_against_schema(schema, _valid_payload(component="something-else"))
    assert any("component" in p for p in problems)


def test_payload_with_abbreviated_sha_fails_schema_pattern():
    schema = _load_schema()
    problems = _check_against_schema(schema, _valid_payload(testedSha="short"))
    assert any("testedSha" in p for p in problems)
