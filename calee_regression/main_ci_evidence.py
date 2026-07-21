"""Independent verification of MERGED-MAIN CI evidence (Priority 8).

A green check on a pull request's HEAD commit is proof about that commit --
never proof about the commit GitHub actually merges (a later commit can land
on ``main`` after the PR's own checks ran, and a merge-queue's synthetic
merge commit can differ from either parent). ``.github/workflows/
framework-tests.yml`` already embeds the exact commit SHA under test into a
retained ``framework-test-summary-<sha>.json`` artifact and re-checks it
in-workflow via a "Merge-commit / main smoke check" step -- but that
in-workflow re-check only proves what THAT SPECIFIC RUN believed about
itself. This module lets a technical owner (or CaleeMobile-Regression's
equivalent stdlib-only script) independently re-verify a DOWNLOADED copy of
that evidence after the fact, offline, without trusting the workflow run
that produced it.

Two evidence shapes are both handled with the SAME verification contract:

  * calee-regression's own simple, single-job shape (``workflow``, ``event``,
    ``ref``, ``commitSha``, ``runId``, ``runAttempt``, ``isMainPush``,
    ``isMergeGroup``) -- one unconditional job, so "every required gate" has
    nothing beyond the evidence's own presence+identity to check;
  * a richer, multi-gate shape (as CaleeMobile-Regression's ``ci-summary.json``
    carries: a ``gates`` mapping of gate-name -> ``needs.*.result``, and a
    ``skipClassification`` mapping of gate-name -> ``"not-applicable"``/
    ``"unexpected"`` for any gate whose result is ``"skipped"``) -- when
    present, every gate named in ``required_gates`` must be ``"success"``, or
    ``"skipped"`` AND classified ``"not-applicable"``; anything else BLOCKS.

Never accepts a ``pull_request`` (or any other non-main-commit) event as
merged-main evidence, regardless of how well-formed the rest of the evidence
otherwise looks -- see :func:`verify_main_ci_evidence`'s ``event``/``ref``
checks.

Priority 5 (this session) versions the contract explicitly (``schemaVersion``)
and gives the CaleeMobile-Regression shape a CANONICAL required-gate set that
lives in code (:data:`CALEEMOBILE_REGRESSION_REPOSITORY` /
:data:`CALEEMOBILE_REGRESSION_REQUIRED_GATES`) -- so a missing/empty/truncated
``gates`` object BLOCKS even when the caller passes no ``--required-gate`` at
all, closing the gap where an evidence file with ``"gates": {}`` (or no
``gates`` key) previously produced zero problems from the gate-checking
section entirely. The SAME canonical set is duplicated (never imported) in
CaleeMobile-Regression's stdlib-only ``api/verify_main_ci_evidence.py``, and a
contract test in that repo reads the workflow file itself and proves the two
agree -- mirroring the ``selector_release_certification.schema.json``
duplicated-schema pattern already used for selector evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# The only two events that can ever describe what's ACTUALLY landing on the
# target branch: a merge-queue's synthetic merge commit, or a direct push
# whose ref is exactly refs/heads/main. Anything else -- most importantly
# "pull_request" -- describes a candidate commit, never the merged result.
MAIN_EVENT_PUSH = "push"
MAIN_EVENT_MERGE_GROUP = "merge_group"
MAIN_REF = "refs/heads/main"

# Priority 5: the only schemaVersion this consumer knows how to read. A
# summary declaring a version outside this set was produced by a newer (or
# older, incompatible) emitter -- refuse it rather than guess, exactly like
# selector_evidence.SUPPORTED_SCHEMA_VERSIONS.
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# Priority 5: CaleeMobile-Regression's canonical required gates, owned HERE in
# code (not just in prose/docs), matching EXACTLY the keys
# `.github/workflows/ci.yml`'s `ci-evidence` job's "Record CI evidence" step
# emits into its `gates` dict -- see that repo's
# api/verify_main_ci_evidence.py (the byte-identical duplicate of this
# constant) and its workflow-contract test. Deliberately camelCase (matching
# the emitter), never GitHub job IDs (which are kebab-case) -- the two must
# not be conflated.
CALEEMOBILE_REGRESSION_REPOSITORY = "CaleeAdmin/CaleeMobile-Regression"
CALEEMOBILE_REGRESSION_WORKFLOW_FILE = ".github/workflows/ci.yml"
CALEEMOBILE_REGRESSION_REQUIRED_GATES = (
    "apiFrameworkTests",
    "uiReportWrapperTests",
    "fixtureCliSmoke",
    "selectorContract",
    "uiSuiteAnalyze",
    "releaseCertificationGuard",
)


class MainCiEvidenceError(Exception):
    """The evidence file is missing, unreadable, or not a JSON object -- a
    framework/pipeline fault, never a verdict (a real problem with the
    evidence's CONTENT is returned as a problem list, not raised)."""


def load_summary(path: "Path | str") -> "tuple[dict, bytes]":
    """Read and parse a CI-summary JSON file. Returns ``(parsed, raw_bytes)``
    -- the raw bytes are what an ``--artifact-sha256`` digest check is
    computed over (never a re-serialised/reparsed form)."""
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise MainCiEvidenceError(f"could not read CI evidence summary at {path}: {exc}") from exc
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MainCiEvidenceError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MainCiEvidenceError(f"{path} must contain a JSON object.")
    return data, raw_bytes


def verify_main_ci_evidence(
    summary: "dict[str, Any]",
    *,
    expected_sha: str,
    required_gates: "list[str] | None" = None,
    raw_bytes: "bytes | None" = None,
    expected_artifact_sha256: "str | None" = None,
    expected_repository: "str | None" = None,
    expected_workflow_file: "str | None" = None,
    canonical_required_gates: "tuple[str, ...] | list[str] | None" = None,
) -> "list[str]":
    """Verify one retained CI-evidence summary describes the EXACT merged-main
    commit expected, with every required gate accounted for. Returns a list
    of problems (empty == accepted). Never raises for a bad/incomplete
    evidence shape -- only :func:`load_summary` raises, for a file that can't
    even be read as JSON.

    ``expected_sha`` must be the full 40-character commit SHA of the actual
    merge/main commit -- retrieved and checked AFTER the merge, never
    predicted or assumed during a PR session (see the module docstring and
    the CLI command's own help text).

    Priority 5: ``expected_repository``/``expected_workflow_file``, when
    given, cross-check the evidence's own ``repository``/``workflowFile``
    fields (present or not, a mismatch BLOCKS). ``canonical_required_gates``,
    when given, is ALWAYS enforced in addition to (union with) any explicit
    ``required_gates`` -- unlike ``required_gates`` alone, this closes the gap
    where an evidence file with no ``gates`` key, or an empty ``gates: {}``,
    produced zero gate-related problems simply because the caller passed no
    ``--required-gate``: with a non-empty canonical set, every one of its
    gates is looked up and reported missing.
    """
    problems: "list[str]" = []

    if not expected_sha or len(expected_sha) != 40 or not all(c in "0123456789abcdefABCDEF" for c in expected_sha):
        problems.append(f"--expected-sha {expected_sha!r} must be the full 40-character commit SHA.")

    schema_version = summary.get("schemaVersion")
    if schema_version is None:
        problems.append("evidence has no schemaVersion recorded.")
    elif not isinstance(schema_version, int) or isinstance(schema_version, bool):
        problems.append(f"evidence schemaVersion must be a JSON integer (got {schema_version!r}).")
    elif schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        problems.append(
            f"evidence schemaVersion {schema_version!r} is not supported (this verifier supports "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}) -- refusing to read evidence produced by an "
            f"unknown emitter version."
        )

    if expected_repository is not None:
        repository = summary.get("repository")
        if not repository:
            problems.append(f"expected repository {expected_repository!r} but evidence has no repository recorded.")
        elif repository != expected_repository:
            problems.append(f"evidence repository {repository!r} != expected {expected_repository!r}.")

    if expected_workflow_file is not None:
        workflow_file = summary.get("workflowFile")
        if not workflow_file:
            problems.append(
                f"expected workflowFile {expected_workflow_file!r} but evidence has no workflowFile recorded."
            )
        elif workflow_file != expected_workflow_file:
            problems.append(f"evidence workflowFile {workflow_file!r} != expected {expected_workflow_file!r}.")

    commit_sha = summary.get("commitSha")
    if not commit_sha:
        problems.append("evidence has no commitSha recorded.")
    elif expected_sha and str(commit_sha).lower() != str(expected_sha).lower():
        problems.append(
            f"evidence commitSha {commit_sha!r} != expected merged-main commit {expected_sha!r} -- this "
            f"evidence is NOT for the commit being verified."
        )

    event = summary.get("event")
    ref = summary.get("ref")
    is_main_push = event == MAIN_EVENT_PUSH and ref == MAIN_REF
    is_merge_group = event == MAIN_EVENT_MERGE_GROUP
    if event == "pull_request":
        problems.append(
            "evidence event is 'pull_request' -- a PR-head check is proof about the PR HEAD commit only, "
            "never about the merged/main commit. Rejected as merged-main evidence."
        )
    elif not (is_main_push or is_merge_group):
        problems.append(
            f"evidence event {event!r} (ref {ref!r}) is neither a push to {MAIN_REF!r} nor a "
            f"{MAIN_EVENT_MERGE_GROUP!r} run -- not merged-main evidence."
        )
    # Cross-check the evidence's OWN boolean flags (when present) agree with
    # its event/ref -- a tampered or hand-edited summary claiming isMainPush
    # while the ref/event say otherwise (or vice versa) is caught here too.
    if "isMainPush" in summary and bool(summary.get("isMainPush")) != is_main_push:
        problems.append(
            f"evidence isMainPush={summary.get('isMainPush')!r} disagrees with its own event/ref "
            f"(event={event!r}, ref={ref!r})."
        )
    if "isMergeGroup" in summary and bool(summary.get("isMergeGroup")) != is_merge_group:
        problems.append(
            f"evidence isMergeGroup={summary.get('isMergeGroup')!r} disagrees with its own event "
            f"({event!r})."
        )

    gates = summary.get("gates")
    skip_classification = summary.get("skipClassification") or {}
    # Priority 5: the EFFECTIVE required-gate set is the union of whatever the
    # caller explicitly asked for (--required-gate, repeatable) and the
    # verifier's OWN canonical set (when the caller identified a consumer --
    # e.g. CaleeMobile-Regression -- that has one). This is what makes a
    # missing required gate BLOCK even when --required-gate was never passed
    # at all: the canonical set is not optional, caller-suppliable input, it
    # is a baseline this verifier owns.
    effective_required_gates = sorted(set(required_gates or ()) | set(canonical_required_gates or ()))
    if effective_required_gates:
        if not isinstance(gates, dict):
            problems.append(
                f"required gate(s) {effective_required_gates} were requested, but this evidence carries no "
                f"'gates' breakdown at all -- cannot verify them."
            )
        elif not gates:
            problems.append(
                f"required gate(s) {effective_required_gates} were requested, but this evidence's 'gates' "
                f"breakdown is empty -- cannot verify them."
            )
        else:
            for gate in effective_required_gates:
                result = gates.get(gate)
                if result is None:
                    problems.append(f"required gate {gate!r} is not present in the evidence's gates.")
                elif result == "success":
                    continue
                elif result == "skipped":
                    classification = skip_classification.get(gate)
                    if classification != "not-applicable":
                        problems.append(
                            f"required gate {gate!r} was skipped and is NOT classified 'not-applicable' "
                            f"(classification: {classification!r}) -- an unexplained/unexpected skip is "
                            f"treated exactly like a failure."
                        )
                else:
                    problems.append(f"required gate {gate!r} did not succeed (result: {result!r}).")
    elif isinstance(gates, dict) and gates:
        # No required-gate set at all (explicit or canonical), but the
        # evidence itself carries a non-empty gates breakdown -- verify EVERY
        # gate it lists, so a caller can't accidentally under-specify and
        # miss a real failure.
        for gate, result in gates.items():
            if result == "success":
                continue
            if result == "skipped" and skip_classification.get(gate) == "not-applicable":
                continue
            tag = " (unexpected skip)" if result == "skipped" else ""
            problems.append(f"gate {gate!r} did not succeed (result: {result!r}{tag}).")

    if expected_artifact_sha256 is not None:
        if raw_bytes is None:
            problems.append("an --artifact-sha256 was given but no raw evidence bytes were supplied to check it against.")
        else:
            actual = hashlib.sha256(raw_bytes).hexdigest()
            if actual.lower() != expected_artifact_sha256.strip().lower():
                problems.append(
                    f"evidence artifact digest mismatch: expected sha256 {expected_artifact_sha256}, "
                    f"actual {actual} -- the retained evidence file's bytes do not match."
                )

    return problems
