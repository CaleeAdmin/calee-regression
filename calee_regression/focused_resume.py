"""Safe focused-verify resume (Phase 7).

`focused-verify --resume-run-id <run-id>` may reuse a prior invocation's
child results ONLY when every reuse criterion can be POSITIVELY verified
against the current invocation context (see :data:`RESUME_CRITERIA`); any
single criterion that fails -- or merely cannot be proven -- refuses the
ENTIRE resume, naming every failed criterion, and the tester is told to start
a fresh focused run instead. There is no partial trust and no bypass flag.

Reuse rules:

  * only prior PASS steps are reusable, referenced by their ORIGINAL report
    path + recomputed-and-matching sha256 digest;
  * a prior product FAIL is never automatically rerun or replaced: it is
    retained as a FAIL in the new invocation's summary unless the tester
    passes an explicit ``--retry-failed``, which reruns it as a NEW attempt
    without deleting the old evidence;
  * BLOCKED / blocked_not_run / invalid_config steps are re-executed;
  * a prior PASS is never copied into a DIFFERENT run id -- resume continues
    the SAME run id with a NEW invocation directory under it; all prior
    evidence stays immutable.

`evaluate_resume` is pure: the file-digest reader is injected so every branch
is unit-testable without a filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import focused_workflow
from .focused_report_validation import sha256_of_file

SUPPORTED_SUMMARY_SCHEMA_VERSIONS = {focused_workflow.SUMMARY_SCHEMA_VERSION}

# Prior statuses that are re-executed on resume (never reused, never refused).
_REEXECUTE_STATUSES = (
    focused_workflow.STATUS_BLOCKED,
    focused_workflow.STATUS_BLOCKED_NOT_RUN,
    focused_workflow.STATUS_INVALID_CONFIG,
)

# The complete criterion vocabulary a refusal names entries from.
CRITERION_SCHEMA = "supported report schema"
CRITERION_RUN_ID = "run identity"
CRITERION_FRAMEWORK_SHAS = "framework SHAs"
CRITERION_PRODUCT_SHAS = "product SHAs"
CRITERION_BACKEND = "backend"
CRITERION_FIXTURE_VERSION = "fixture version"
CRITERION_FIXTURE_OWNERSHIP = "fixture ownership"
CRITERION_FIXTURE_IDENTITY = "fixture generation/reset identity"
CRITERION_DEVICE_ID = "device id"
CRITERION_INSTALLED_BUILD = "installed build identity"
CRITERION_EXECUTION_PURPOSE = "execution purpose"
CRITERION_FEATURE_SCOPE = "feature scope"
CRITERION_CHILD_DIGEST = "child report digest"

RESUME_CRITERIA = (
    CRITERION_SCHEMA, CRITERION_RUN_ID, CRITERION_FRAMEWORK_SHAS,
    CRITERION_PRODUCT_SHAS, CRITERION_BACKEND, CRITERION_FIXTURE_VERSION,
    CRITERION_FIXTURE_OWNERSHIP, CRITERION_FIXTURE_IDENTITY,
    CRITERION_DEVICE_ID, CRITERION_INSTALLED_BUILD,
    CRITERION_EXECUTION_PURPOSE, CRITERION_FEATURE_SCOPE,
    CRITERION_CHILD_DIGEST,
)

FRESH_RUN_COMMAND = "python3 -m calee_regression focused-verify --config config/tester.local.yaml"

EVIDENCE_REUSED = "reused"
EVIDENCE_EXECUTED = "executed"


@dataclass
class ResumeContext:
    """The CURRENT invocation's verified identity a prior summary is compared
    against. Every field must be POSITIVELY known -- a None where the prior
    summary recorded a value means the criterion cannot be verified, which
    refuses the resume."""

    run_id: str
    backend: "str | None" = None
    fixture_version: "str | None" = None
    regression_shas: dict = field(default_factory=dict)
    product_sha: "str | None" = None
    device_ids: dict = field(default_factory=dict)
    installed_artifact: dict = field(default_factory=dict)
    execution_purpose: "str | None" = None
    # The step ids the current invocation would run (its feature scope).
    step_ids: "tuple | list" = ()
    # Positively-proven statement that no other owner has held the fixture
    # lock since the prior run (host-local evidence). None = unprovable.
    lock_history_clean: "bool | None" = None
    # True when the fixture the resumed children depend on would need a
    # re-reset (invalidating all fixture-dependent prior evidence).
    fixture_needs_reset: bool = False


@dataclass
class ReusableStep:
    id: str
    title: str
    status: str
    report_path: "str | None"
    report_sha256: "str | None"
    mode: "str | None" = None
    detail: str = ""

    def to_result(self) -> "focused_workflow.FocusedResult":
        return focused_workflow.FocusedResult(
            id=self.id, title=self.title, status=self.status, exit_code=None,
            mode=self.mode, detail=self.detail, report_path=self.report_path,
            report_sha256=self.report_sha256, evidence=EVIDENCE_REUSED,
        )


@dataclass
class ResumeDecision:
    eligible: bool
    failed_criteria: "list[str]" = field(default_factory=list)
    reasons: "list[str]" = field(default_factory=list)
    reused: "list[ReusableStep]" = field(default_factory=list)
    retained_failures: "list[ReusableStep]" = field(default_factory=list)
    execute_step_ids: "list[str]" = field(default_factory=list)

    def refusal_message(self) -> str:
        named = ", ".join(self.failed_criteria) or "unknown"
        details = "\n".join(f"  - {r}" for r in self.reasons)
        return (
            "BLOCKED: focused resume refused -- the following criteria could not "
            f"be positively verified: {named}.\n{details}\n"
            "Prior evidence is untouched. Start a fresh focused run instead:\n"
            f"  {FRESH_RUN_COMMAND}"
        )


def _check(problems: "list[tuple[str, str]]", criterion: str, ok: "bool | None", detail: str) -> None:
    """ok=True passes; False fails; None means 'cannot be positively
    verified', which fails identically (fail-closed)."""
    if ok is not True:
        suffix = "could not be positively verified" if ok is None else "does not match"
        problems.append((criterion, f"{criterion} {suffix}: {detail}"))


def _known_equal(prior, current) -> "bool | None":
    """Equality that refuses to claim a match when either side is unknown."""
    if prior in (None, {}, ()) or current in (None, {}, ()):
        return None
    return prior == current


def evaluate_resume(
    prior_summary: dict,
    current: ResumeContext,
    *,
    file_digest=sha256_of_file,
    retry_failed: bool = False,
) -> ResumeDecision:
    """Decide whether (and exactly which of) a prior invocation's results may
    be reused. Pure; ``file_digest(Path) -> str | None`` is injected."""
    problems: "list[tuple[str, str]]" = []

    schema_ok = (
        prior_summary.get("reportType") == focused_workflow.SUMMARY_REPORT_TYPE
        and prior_summary.get("reportSchemaVersion") in SUPPORTED_SUMMARY_SCHEMA_VERSIONS
    )
    _check(problems, CRITERION_SCHEMA, schema_ok,
           f"prior summary is {prior_summary.get('reportType')!r} "
           f"v{prior_summary.get('reportSchemaVersion')!r}; supported: "
           f"{focused_workflow.SUMMARY_REPORT_TYPE!r} v{sorted(SUPPORTED_SUMMARY_SCHEMA_VERSIONS)}")
    _check(problems, CRITERION_RUN_ID,
           _known_equal(prior_summary.get("runId"), current.run_id),
           f"prior {prior_summary.get('runId')!r} vs current {current.run_id!r} -- a prior "
           "PASS is never copied into a different run id")
    _check(problems, CRITERION_FRAMEWORK_SHAS,
           _known_equal(prior_summary.get("regressionShas"), dict(current.regression_shas)),
           f"prior {prior_summary.get('regressionShas')!r} vs current {dict(current.regression_shas)!r}")
    prior_product_sha = (prior_summary.get("productBuild") or {}).get("caleeMobileSha")
    _check(problems, CRITERION_PRODUCT_SHAS,
           _known_equal(prior_product_sha, current.product_sha),
           f"prior {prior_product_sha!r} vs current {current.product_sha!r}")
    _check(problems, CRITERION_BACKEND,
           _known_equal(prior_summary.get("verifiedBackend"), current.backend),
           f"prior {prior_summary.get('verifiedBackend')!r} vs current {current.backend!r}")
    _check(problems, CRITERION_FIXTURE_VERSION,
           _known_equal(prior_summary.get("fixtureVersion"), current.fixture_version),
           f"prior {prior_summary.get('fixtureVersion')!r} vs current {current.fixture_version!r}")

    ownership = prior_summary.get("fixtureOwnership") or {}
    prior_lock_ok = (
        (ownership.get("acquisition") or {}).get("state") == "acquired"
        and (ownership.get("release") or {}).get("state") == "released"
    )
    ownership_ok = current.lock_history_clean if prior_lock_ok else False
    _check(problems, CRITERION_FIXTURE_OWNERSHIP, ownership_ok,
           "the prior summary's lock evidence and the current lock state must prove no "
           "other owner has held the fixture lock since")

    fixture_ok: "bool | None"
    prior_steps = [s for s in prior_summary.get("steps") or [] if isinstance(s, dict)]
    by_id = {s.get("id"): s for s in prior_steps}
    fixture_step = by_id.get("fixture")
    if current.fixture_needs_reset:
        fixture_ok = False
        fixture_detail = "the fixture would need a re-reset -- all fixture-dependent prior evidence is invalid"
    elif not fixture_step or fixture_step.get("status") != focused_workflow.STATUS_PASS:
        fixture_ok = False
        fixture_detail = "the prior invocation has no passing fixture-preparation step"
    else:
        path = fixture_step.get("reportPath")
        recorded = fixture_step.get("reportSha256")
        actual = file_digest(Path(path)) if path else None
        fixture_ok = bool(path and recorded and actual == recorded)
        fixture_detail = (
            f"fixture-preparation report {path!r} must still exist and digest-match "
            f"{recorded!r} (got {actual!r})"
        )
    _check(problems, CRITERION_FIXTURE_IDENTITY, fixture_ok, fixture_detail)

    _check(problems, CRITERION_DEVICE_ID,
           _known_equal(prior_summary.get("deviceIds"), dict(current.device_ids)),
           f"prior {prior_summary.get('deviceIds')!r} vs current {dict(current.device_ids)!r}")

    prior_artifact = prior_summary.get("installedArtifactIdentity") or {}
    current_artifact = dict(current.installed_artifact)
    if prior_artifact.get("status") == "verified" and current_artifact.get("status") == "verified":
        artifact_ok = _known_equal(
            prior_artifact.get("installed") or prior_artifact.get("expected"),
            current_artifact.get("installed") or current_artifact.get("expected"),
        )
    else:
        artifact_ok = None
    _check(problems, CRITERION_INSTALLED_BUILD, artifact_ok,
           f"prior status {prior_artifact.get('status')!r} vs current "
           f"{current_artifact.get('status')!r} -- both must be verified and identical")

    _check(problems, CRITERION_EXECUTION_PURPOSE,
           _known_equal(prior_summary.get("executionPurpose"), current.execution_purpose),
           f"prior {prior_summary.get('executionPurpose')!r} vs current {current.execution_purpose!r}")

    prior_scope = sorted(by_id)
    current_scope = sorted(set(current.step_ids) | {"fixture"})
    _check(problems, CRITERION_FEATURE_SCOPE,
           _known_equal(prior_scope or None, current_scope or None),
           f"prior steps {prior_scope!r} vs current {current_scope!r}")

    # Child report digests: every prior PASS (and every retained FAIL) must
    # still be backed by its original, byte-identical report file.
    reused: "list[ReusableStep]" = []
    retained: "list[ReusableStep]" = []
    execute: "list[str]" = []
    for step in prior_steps:
        step_id = step.get("id")
        status = step.get("status")
        if step_id == "fixture":
            continue  # verified above as the fixture-identity criterion
        if status in _REEXECUTE_STATUSES:
            execute.append(step_id)
            continue
        path = step.get("reportPath")
        recorded = step.get("reportSha256")
        actual = file_digest(Path(path)) if path else None
        digest_ok = bool(path and recorded and actual == recorded)
        if status == focused_workflow.STATUS_FAIL and retry_failed:
            # An explicit new attempt -- old evidence stays where it is.
            execute.append(step_id)
            continue
        if not digest_ok:
            _check(problems, CRITERION_CHILD_DIGEST, False,
                   f"step {step_id!r} report {path!r} must still exist and digest-match "
                   f"{recorded!r} (got {actual!r})")
            continue
        entry = ReusableStep(
            id=step_id, title=step.get("title") or step_id, status=status,
            report_path=path, report_sha256=recorded, mode=step.get("mode"),
        )
        if status == focused_workflow.STATUS_PASS:
            entry.detail = "reused from prior invocation (all resume criteria verified)"
            reused.append(entry)
        elif status == focused_workflow.STATUS_FAIL:
            entry.detail = (
                "retained prior product FAIL -- never automatically rerun or replaced; "
                "pass --retry-failed to rerun it as a NEW attempt"
            )
            retained.append(entry)
        else:
            execute.append(step_id)

    if problems:
        seen: "list[str]" = []
        for criterion, _detail in problems:
            if criterion not in seen:
                seen.append(criterion)
        return ResumeDecision(
            eligible=False, failed_criteria=seen,
            reasons=[detail for _c, detail in problems],
        )
    return ResumeDecision(
        eligible=True, reused=reused, retained_failures=retained,
        execute_step_ids=execute,
    )
