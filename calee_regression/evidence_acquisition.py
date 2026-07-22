"""Exact-identity release evidence acquisition (fail-closed).

Removes the manual step where a technical owner copies GitHub workflow run
IDs, artifact IDs and local ZIP paths around before release qualification.
Instead, every expected identity is derived from things this framework has
already verified:

  1. the verified release bundle (``release_installer.verify_release_bundle``);
  2. the frozen release candidate / the run's recorded immutable baseline
     (``resume_release`` attempt-1 immutable inputs, when the run has one);
  3. the effective release configuration (schema-v2 bundle manifest scope).

and the exact matching GitHub Actions evidence is located, authenticated and
cached in the release run workspace, with a secret-free acquisition manifest.

What is *never* done here:

  * "use the latest successful run" -- selection is by exact repository,
    exact workflow path, approved event, exact head SHA / release tuple, and
    EXACTLY ONE matching artifact; zero or multiple matches are BLOCKED;
  * trusting artifact JSON before its GitHub origin (run ownership + recorded
    digest) is authenticated;
  * an unauthenticated fallback when no token is available -- that is BLOCKED,
    naming the missing secret;
  * logging or persisting the token (it lives in request headers only, and
    the manifest schema has no field that could carry it).

Layering: search/selection/verdict logic is pure and driven by an injected
``GithubEvidenceClient`` so every rule is offline-testable; the live client is
the only code that touches the network, and it validates every URL (and every
download redirect) against approved hosts.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import github_artifact as ga
from . import main_ci_artifact as mca
from . import main_ci_evidence as mce
from . import provider_evidence as pe
from . import selector_evidence as se
from .identity_format import is_full_git_sha, is_wellformed_version

ACQUISITION_SCHEMA_VERSION = 1
ACQUISITION_COMPONENT = "release-evidence-acquisition"
ACQUISITION_VERSION = "1.0.0"

MANIFEST_FILENAME = "acquisition-manifest.json"
EVIDENCE_DIRNAME = "evidence"
ACQUIRED_DIRNAME = "acquired"

# Evidence types (stable identifiers used in cache filenames + the manifest).
TYPE_CALEE_REGRESSION_MAIN_CI = "calee-regression-main-ci"
TYPE_CALEEMOBILE_REGRESSION_MAIN_CI = "caleemobile-regression-main-ci"
TYPE_SELECTOR_CERTIFICATION = "selector-certification"
TYPE_DISTRIBUTED_BUILD_ANDROID = "distributed-build-android"
TYPE_DISTRIBUTED_BUILD_IOS = "distributed-build-ios"

# Item statuses.
STATUS_ACQUIRED = "acquired"
STATUS_REUSED_CACHE = "reused-cache"
STATUS_BLOCKED = "blocked"
STATUS_CONTRADICTED = "contradicted"
STATUS_NOT_APPLICABLE = "not_applicable"

# Evidence sources (how the identity was obtained).
SOURCE_AUTOMATIC = "automatic"
SOURCE_EXPLICIT_OVERRIDE = "explicit-override"
SOURCE_CACHE = "cache"
SOURCE_RECORDED = "recorded-evidence"

# Bounded search: never scan an unbounded run history. The live client also
# bounds the query server-side (created >= now - window).
DEFAULT_SEARCH_WINDOW_DAYS = 90
MAX_RUN_CANDIDATES = 50
MAX_SELECTOR_DOWNLOAD_CANDIDATES = 10

# Approved hosts. GitHub API calls must target the configured API host;
# artifact-download redirects may only land on GitHub-owned storage.
APPROVED_REDIRECT_HOST_SUFFIXES = (
    ".github.com",
    ".githubusercontent.com",
    ".windows.net",  # GitHub Actions artifact storage (Azure blob)
)


class AcquisitionUsageError(Exception):
    """Invalid usage / malformed configuration (exit 2): a bad run id, an
    unverifiable bundle, a malformed effective configuration. Raised BEFORE
    any GitHub lookup or download."""


class AcquisitionError(Exception):
    """A structural acquisition fault (exit 3 / BLOCKED): missing credentials,
    an unreachable API, an unwritable workspace. Never a product failure."""


# --- injected GitHub client ---------------------------------------------------


class GithubEvidenceClient:
    """Protocol for the injected client. All methods return parsed GitHub
    REST shapes (plain dicts) or raw ZIP bytes. Tests supply fakes; the live
    implementation is :class:`LiveGithubClient`."""

    def list_workflow_runs(self, repository: str, workflow_file: str, *,
                           head_sha: "str | None" = None,
                           event: "str | None" = None,
                           branch: "str | None" = None) -> "list[dict]":
        raise NotImplementedError

    def get_workflow_run(self, repository: str, run_id: str) -> dict:
        raise NotImplementedError

    def list_run_artifacts(self, repository: str, run_id: str) -> "list[dict]":
        raise NotImplementedError

    def get_artifact(self, repository: str, artifact_id: str) -> dict:
        raise NotImplementedError

    def download_artifact_zip(self, repository: str, artifact_id: str) -> bytes:
        raise NotImplementedError


def validate_github_api_url(url: str, *, api_base: "str | None" = None) -> "list[str]":
    """Structural + approved-host validation for a GitHub API URL."""
    problems: "list[str]" = []
    base = (api_base or (os.environ.get("GITHUB_API_URL") or "https://api.github.com")).rstrip("/")
    base_host = (urlsplit(base).hostname or "").lower()
    parts = urlsplit(url)
    if parts.scheme != "https":
        problems.append(f"GitHub API URL {url!r} is not https.")
    host = (parts.hostname or "").lower()
    if not host:
        problems.append(f"GitHub API URL {url!r} has no host.")
    elif host != base_host:
        problems.append(f"GitHub API URL host {host!r} != approved API host {base_host!r}.")
    if parts.username or parts.password:
        problems.append(f"GitHub API URL {url!r} must not carry userinfo.")
    return problems


def is_approved_redirect_host(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    return any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in APPROVED_REDIRECT_HOST_SUFFIXES)


class LiveGithubClient(GithubEvidenceClient):
    """The only network-touching implementation. The token is held privately,
    sent only as a request header to the approved API host, and NEVER placed
    in argv, logs, or the manifest. Artifact-download redirects are followed
    manually: the redirect target must be an approved GitHub storage host and
    the Authorization header is NOT forwarded to it."""

    def __init__(self, token: str, *, window_days: int = DEFAULT_SEARCH_WINDOW_DAYS,
                 now: "datetime | None" = None):
        self._token = token
        self._base = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self._window_days = window_days
        self._now = now

    def __repr__(self) -> str:  # never leak the token
        return f"LiveGithubClient(base={self._base!r})"

    __str__ = __repr__

    def _get(self, url: str, *, expect_json: bool = True, authorized: bool = True,
             max_bytes: "int | None" = None) -> Any:
        import urllib.request

        problems = validate_github_api_url(url, api_base=self._base) if authorized else []
        if authorized and problems:
            raise AcquisitionError("refusing GitHub request: " + " ".join(problems))
        headers = dict(ga._api_headers(self._token)) if authorized else {
            "Accept": "application/vnd.github+json",
            "User-Agent": "calee-regression-evidence-acquisition",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - host validated above
                data = resp.read((max_bytes + 1) if max_bytes else -1)
        except Exception as exc:  # noqa: BLE001 - surfaced as BLOCKED
            raise AcquisitionError(f"GitHub request failed for {url}: {exc}") from exc
        if expect_json:
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AcquisitionError(f"GitHub response for {url} is not valid JSON: {exc}") from exc
        return data

    def list_workflow_runs(self, repository, workflow_file, *, head_sha=None, event=None, branch=None):
        from urllib.parse import quote, urlencode

        params = {"per_page": str(MAX_RUN_CANDIDATES), "status": "completed"}
        if head_sha:
            params["head_sha"] = head_sha
        if event:
            params["event"] = event
        if branch:
            params["branch"] = branch
        if self._window_days:
            now = self._now or datetime.now(timezone.utc)
            since = now.timestamp() - self._window_days * 86400
            created = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d")
            params["created"] = f">={created}"
        name = quote(Path(workflow_file).name)
        url = f"{self._base}/repos/{repository}/actions/workflows/{name}/runs?{urlencode(params)}"
        data = self._get(url)
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        return [r for r in (runs or []) if isinstance(r, dict)]

    def get_workflow_run(self, repository, run_id):
        data = self._get(f"{self._base}/repos/{repository}/actions/runs/{run_id}")
        if not isinstance(data, dict):
            raise AcquisitionError("workflow-run metadata is not a JSON object.")
        return data

    def list_run_artifacts(self, repository, run_id):
        data = self._get(f"{self._base}/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = data.get("artifacts") if isinstance(data, dict) else None
        return [a for a in (artifacts or []) if isinstance(a, dict)]

    def get_artifact(self, repository, artifact_id):
        data = self._get(f"{self._base}/repos/{repository}/actions/artifacts/{artifact_id}")
        if not isinstance(data, dict):
            raise AcquisitionError("artifact metadata is not a JSON object.")
        return data

    def download_artifact_zip(self, repository, artifact_id):
        import urllib.error
        import urllib.request

        url = f"{self._base}/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        req = urllib.request.Request(url, headers=ga._api_headers(self._token))

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N803
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        location: "str | None" = None
        try:
            with opener.open(req, timeout=120) as resp:
                # 200 directly (unusual but valid).
                return resp.read(ga.MAX_ARTIFACT_ZIP_BYTES + 1)
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
            else:
                raise AcquisitionError(f"GitHub artifact download failed ({exc.code}) for {url}.") from exc
        except Exception as exc:  # noqa: BLE001
            raise AcquisitionError(f"GitHub artifact download failed for {url}: {exc}") from exc
        if not location or not is_approved_redirect_host(location):
            raise AcquisitionError(
                "GitHub artifact download redirected to an unapproved host -- refusing to follow."
            )
        # The redirect target is a pre-signed URL: fetch WITHOUT the token.
        return self._get(location, expect_json=False, authorized=False,
                         max_bytes=ga.MAX_ARTIFACT_ZIP_BYTES)


# --- expected-identity plan ---------------------------------------------------


@dataclass
class EvidenceSpec:
    """One expected evidence item, fully derived before any GitHub call."""

    evidence_type: str
    required: bool
    repository: "str | None" = None
    workflow_file: "str | None" = None
    approved_events: "tuple[str, ...]" = ()
    required_branch: "str | None" = None
    expected_head_sha: "str | None" = None
    artifact_name: "str | None" = None
    result_filename: "str | None" = None
    # Release tuple (selector / distributed-build).
    release_id: "str | None" = None
    expected_product_sha: "str | None" = None
    expected_version: "str | None" = None
    platform: "str | None" = None
    canonical_required_gates: "tuple[str, ...] | None" = None
    not_applicable_reason: "str | None" = None
    derivation: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "evidenceType": self.evidence_type,
            "required": self.required,
            "repository": self.repository,
            "workflowFile": self.workflow_file,
            "approvedEvents": list(self.approved_events),
            "requiredBranch": self.required_branch,
            "expectedHeadSha": self.expected_head_sha,
            "artifactName": self.artifact_name,
            "resultFilename": self.result_filename,
            "releaseId": self.release_id,
            "expectedProductSha": self.expected_product_sha,
            "expectedVersion": self.expected_version,
            "platform": self.platform,
            "notApplicableReason": self.not_applicable_reason,
            "derivation": self.derivation,
        }


@dataclass
class EvidencePlan:
    run_id: str
    release_id: "str | None"
    bundle_path: str
    specs: "list[EvidenceSpec]" = field(default_factory=list)
    problems: "list[str]" = field(default_factory=list)

    def spec(self, evidence_type: str) -> "EvidenceSpec | None":
        for s in self.specs:
            if s.evidence_type == evidence_type:
                return s
        return None


def _framework_shas_from_workspace(workspace_root: Path) -> "dict[str, str | None]":
    """Framework SHAs from the run's recorded immutable baseline (attempt 1),
    when it exists. Recorded evidence beats any live checkout state."""
    baseline = workspace_root / "attempts" / "1" / "immutable-inputs.json"
    out: "dict[str, str | None]" = {"regressionSha": None, "caleeMobileRegressionSha": None}
    if not baseline.is_file():
        return out
    try:
        data = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if isinstance(data, dict):
        for key in out:
            value = data.get(key)
            if isinstance(value, str) and is_full_git_sha(value):
                out[key] = value
    return out


def _git_head_sha(repo_dir: "Path | None") -> "str | None":
    """The SHA a checkout is at. Used ONLY when the run has no recorded
    baseline yet: the checkouts feeding a brand-new release run are the same
    inputs the immutable baseline will record for it."""
    if repo_dir is None or not (repo_dir / ".git").exists():
        return None
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and is_full_git_sha(sha) else None


def derive_evidence_plan(
    *,
    bundle_path: Path,
    run_id: str,
    repo_root: Path,
    report_root: Path,
    sibling_regression_root: "Path | None" = None,
    verification: "Any | None" = None,
) -> EvidencePlan:
    """Derive every expected identity from the verified bundle + effective
    configuration + the run's recorded baseline. Raises
    :class:`AcquisitionUsageError` (exit 2) when the bundle or configuration
    is invalid -- BEFORE any download could happen."""
    from . import release_installer
    from . import run_context

    if not run_context.is_valid_run_id(run_id):
        raise AcquisitionUsageError(f"invalid run id {run_id!r}.")
    if verification is None:
        verification = release_installer.verify_release_bundle(bundle_path)
    if not getattr(verification, "ok", False):
        errors = getattr(verification, "errors", None) or ["bundle verification failed"]
        raise AcquisitionUsageError(
            "release bundle failed verification -- refusing to derive evidence "
            "identities from it: " + "; ".join(str(e) for e in errors)
        )
    manifest = getattr(verification, "manifest", None)
    if manifest is None or not getattr(manifest, "release_id", None):
        raise AcquisitionUsageError("verified bundle has no manifest releaseId.")

    workspace_root = Path(report_root) / "reports" / "runs" / run_id
    recorded = _framework_shas_from_workspace(workspace_root)
    regression_sha = recorded["regressionSha"] or _git_head_sha(repo_root)
    sibling = sibling_regression_root
    if sibling is None:
        sibling = Path(repo_root).parent / "CaleeMobile-Regression"
    caleemobile_regression_sha = recorded["caleeMobileRegressionSha"] or _git_head_sha(sibling)
    derivation_fw = (
        "recorded immutable baseline (attempts/1/immutable-inputs.json)"
        if recorded["regressionSha"] else "release checkout HEAD (no recorded baseline yet)"
    )

    calee_mobile = getattr(manifest, "calee_mobile", None)
    platforms = getattr(manifest, "platforms", None)
    release_id = manifest.release_id
    plan = EvidencePlan(run_id=run_id, release_id=release_id, bundle_path=str(bundle_path))

    # 1 + 2: merged-main CI evidence for both regression repositories.
    for evidence_type, repository, sha in (
        (TYPE_CALEE_REGRESSION_MAIN_CI, "CaleeAdmin/calee-regression", regression_sha),
        (TYPE_CALEEMOBILE_REGRESSION_MAIN_CI, mce.CALEEMOBILE_REGRESSION_REPOSITORY,
         caleemobile_regression_sha),
    ):
        profile = mca.KNOWN_PROFILES[repository]
        canonical = (
            tuple(mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES)
            if repository == mce.CALEEMOBILE_REGRESSION_REPOSITORY else None
        )
        plan.specs.append(EvidenceSpec(
            evidence_type=evidence_type,
            required=True,
            repository=repository,
            workflow_file=profile["workflow_path"],
            approved_events=(mce.MAIN_EVENT_PUSH, mce.MAIN_EVENT_MERGE_GROUP),
            required_branch="main",
            expected_head_sha=sha,
            artifact_name=(profile["artifact_prefix"] + sha) if sha else None,
            result_filename=profile["result_filename"],
            canonical_required_gates=canonical,
            derivation=derivation_fw,
        ))
        if sha is None:
            plan.problems.append(
                f"{evidence_type}: expected framework SHA could not be derived from the run's "
                f"recorded baseline or the release checkout -- acquisition for this item is blocked."
            )

    # 3: selector certification.
    selector_required = True
    if calee_mobile is not None:
        selector_required = bool(calee_mobile.selector_evidence_required)
    mobile_in_scope = platforms is None or platforms.mobile_android or platforms.mobile_ios
    cm_sha = getattr(calee_mobile, "git_sha", None)
    cm_version = getattr(calee_mobile, "version", None)
    if selector_required and mobile_in_scope:
        if not (cm_sha and is_full_git_sha(cm_sha)) or not (cm_version and is_wellformed_version(cm_version)):
            raise AcquisitionUsageError(
                "bundle manifest requires selector evidence but carries no exact CaleeMobile "
                "gitSha/version to match it against (schema-v2 caleeMobile block required)."
            )
        plan.specs.append(EvidenceSpec(
            evidence_type=TYPE_SELECTOR_CERTIFICATION,
            required=True,
            repository=ga.EXPECTED_WORKFLOW_REPO,
            workflow_file=ga.EXPECTED_WORKFLOW_PATH,
            approved_events=tuple(sorted(ga.PRODUCTION_DISPATCH_EVENTS)),
            artifact_name=ga.EXPECTED_ARTIFACT_NAME,
            result_filename=ga.EXPECTED_RESULT_FILENAME,
            release_id=release_id,
            expected_product_sha=cm_sha,
            expected_version=cm_version,
            derivation="verified bundle manifest caleeMobile block",
        ))
    else:
        plan.specs.append(EvidenceSpec(
            evidence_type=TYPE_SELECTOR_CERTIFICATION, required=False,
            not_applicable_reason="selector evidence not required for this release scope.",
        ))

    # 4: distributed-build evidence per required mobile platform.
    dba_required = True
    if calee_mobile is not None:
        dba_required = bool(calee_mobile.distributed_build_acceptance_required)
    for evidence_type, platform, in_scope in (
        (TYPE_DISTRIBUTED_BUILD_ANDROID, pe.PLATFORM_ANDROID,
         platforms is None or platforms.mobile_android),
        (TYPE_DISTRIBUTED_BUILD_IOS, pe.PLATFORM_IOS,
         platforms is None or platforms.mobile_ios),
    ):
        if dba_required and in_scope:
            plan.specs.append(EvidenceSpec(
                evidence_type=evidence_type, required=True, platform=platform,
                release_id=release_id, expected_product_sha=cm_sha,
                expected_version=cm_version,
                derivation="verified bundle manifest (platform scope + caleeMobile identity)",
            ))
        else:
            reason = ("distributed-build acceptance not required by the release manifest."
                      if not dba_required else f"platform {platform} is out of scope for this release.")
            plan.specs.append(EvidenceSpec(
                evidence_type=evidence_type, required=False, platform=platform,
                not_applicable_reason=reason,
            ))

    return plan


# --- acquired-item record + manifest ------------------------------------------


@dataclass
class AcquiredItem:
    spec: EvidenceSpec
    status: str
    source: "str | None" = None
    problems: "list[str]" = field(default_factory=list)
    remediation: "str | None" = None
    run_data: "dict | None" = None
    artifact_data: "dict | None" = None
    github_digest: "str | None" = None
    observed_digest: "str | None" = None
    cached_path: "str | None" = None
    verified_at: "str | None" = None
    verification_result: "str | None" = None
    result: "dict | None" = None

    def to_dict(self) -> dict:
        run = self.run_data or {}
        artifact = self.artifact_data or {}
        repo = self.spec.repository
        run_id = run.get("id")
        artifact_id = artifact.get("id")
        urls: "dict[str, str]" = {}
        if isinstance(run.get("html_url"), str):
            urls["run"] = run["html_url"]
        elif repo and run_id is not None:
            urls["run"] = f"https://github.com/{repo}/actions/runs/{run_id}"
        if repo and run_id is not None and artifact_id is not None:
            urls["artifact"] = f"https://github.com/{repo}/actions/runs/{run_id}/artifacts/{artifact_id}"
        return {
            "evidenceType": self.spec.evidence_type,
            "required": self.spec.required,
            "status": self.status,
            "source": self.source,
            "repository": repo,
            "workflowName": run.get("name"),
            "workflowFile": self.spec.workflow_file,
            "event": run.get("event"),
            "branch": run.get("head_branch"),
            "headSha": run.get("head_sha"),
            "workflowRunId": str(run_id) if run_id is not None else None,
            "runAttempt": run.get("run_attempt"),
            "conclusion": run.get("conclusion"),
            "artifactId": str(artifact_id) if artifact_id is not None else None,
            "artifactName": artifact.get("name"),
            "artifactExpiresAt": artifact.get("expires_at"),
            "urls": urls,
            "githubDigest": self.github_digest,
            "observedDigest": self.observed_digest,
            "cachedPath": self.cached_path,
            "releaseId": self.spec.release_id,
            "expectedProductSha": self.spec.expected_product_sha,
            "expectedVersion": self.spec.expected_version,
            "platform": self.spec.platform,
            "verificationResult": self.verification_result,
            "verifiedAt": self.verified_at,
            "verifierVersion": ACQUISITION_VERSION,
            "problems": list(self.problems),
            "remediation": self.remediation,
        }


@dataclass
class AcquisitionOutcome:
    plan: EvidencePlan
    items: "list[AcquiredItem]" = field(default_factory=list)
    manifest_path: "str | None" = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def exit_code(self) -> int:
        if any(i.status == STATUS_CONTRADICTED for i in self.items):
            return 1
        if any(i.status == STATUS_BLOCKED and i.spec.required for i in self.items):
            return 3
        return 0

    def item(self, evidence_type: str) -> "AcquiredItem | None":
        for i in self.items:
            if i.spec.evidence_type == evidence_type:
                return i
        return None

    def to_manifest_dict(self, *, generated_at: "str | None" = None) -> dict:
        return {
            "schemaVersion": ACQUISITION_SCHEMA_VERSION,
            "component": ACQUISITION_COMPONENT,
            "runId": self.plan.run_id,
            "releaseId": self.plan.release_id,
            "bundlePath": self.plan.bundle_path,
            "generatedAt": generated_at or _utc_now_iso(),
            "verifierVersion": ACQUISITION_VERSION,
            "planProblems": list(self.plan.problems),
            "items": [i.to_dict() for i in self.items],
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- hardened cache -----------------------------------------------------------

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(text: str) -> str:
    return _SAFE_COMPONENT_RE.sub("-", text)


def evidence_dir(report_root: Path, run_id: str) -> Path:
    return Path(report_root) / "reports" / "runs" / run_id / EVIDENCE_DIRNAME


def cache_path_for(report_root: Path, run_id: str, *, evidence_type: str,
                   repository: str, workflow_run_id: str, artifact_id: str,
                   suffix: str = ".zip") -> Path:
    """Immutable cache file name embedding the evidence type, repository, run
    id and artifact id -- a cache entry can never be mistaken for another
    identity, another run, or another release."""
    name = "--".join((
        _safe_component(evidence_type),
        _safe_component(repository.replace("/", "-")),
        f"run{_safe_component(str(workflow_run_id))}",
        f"art{_safe_component(str(artifact_id))}",
    )) + suffix
    return evidence_dir(report_root, run_id) / ACQUIRED_DIRNAME / name


def write_atomic_private(path: Path, data: bytes) -> None:
    """Atomic, private (0600) write: temp file in the same directory, fsync,
    ``os.replace``. A crash never leaves a partial file at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _ensure_inside(base: Path, candidate: Path) -> None:
    base_resolved = base.resolve()
    resolved = candidate.resolve()
    if base_resolved != resolved and base_resolved not in resolved.parents:
        raise AcquisitionError(
            f"cache path {candidate} escapes the run workspace {base} -- refusing."
        )


def load_cached_zip(path: Path, *, expected_digest_hex: "str | None") -> "bytes | None":
    """Reuse a cached ZIP only when it exists, is a regular non-symlink file,
    and its recomputed sha256 equals GitHub's freshly re-fetched recorded
    digest. Anything else returns None (redownload)."""
    if expected_digest_hex is None:
        return None
    try:
        if not path.is_file() or path.is_symlink():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > ga.MAX_ARTIFACT_ZIP_BYTES:
        return None
    if ga.sha256_hex(data) != expected_digest_hex:
        return None
    return data


def _clean_interrupted_writes(directory: Path) -> None:
    try:
        for leftover in directory.glob("*.tmp"):
            leftover.unlink(missing_ok=True)
    except OSError:
        pass


# --- shared origin checks -----------------------------------------------------


def _digest_hex(artifact_data: dict) -> "str | None":
    digest = artifact_data.get("digest")
    if not digest:
        return None
    text = str(digest).strip().lower()
    return text[len("sha256:"):] if text.startswith("sha256:") else text


def _run_matches_spec(run: dict, spec: EvidenceSpec) -> "list[str]":
    """Exact-identity filters for one candidate workflow run. Every reason a
    run is NOT the expected one, spelled out."""
    problems: "list[str]" = []
    repo = run.get("repository") or {}
    repo_full = repo.get("full_name") if isinstance(repo, dict) else None
    if (repo_full or "").strip() != spec.repository:
        problems.append(f"run repository {repo_full!r} != expected {spec.repository!r}.")
    if (str(run.get("path") or "")).strip() != spec.workflow_file:
        problems.append(f"run workflow path {run.get('path')!r} != approved {spec.workflow_file!r}.")
    event = (str(run.get("event") or "")).strip()
    if event not in spec.approved_events:
        problems.append(
            f"run event {event!r} is not an approved event ({sorted(spec.approved_events)}) -- "
            f"PR-head and workflow-dispatch runs never satisfy merged-main evidence."
            if spec.evidence_type.endswith("main-ci") else
            f"run event {event!r} is not an approved event ({sorted(spec.approved_events)})."
        )
    if spec.required_branch and event == mce.MAIN_EVENT_PUSH:
        if (str(run.get("head_branch") or "")).strip() != spec.required_branch:
            problems.append(
                f"run branch {run.get('head_branch')!r} != required {spec.required_branch!r}."
            )
    if (str(run.get("status") or "")).strip().lower() != "completed":
        problems.append(f"run has not completed (status={run.get('status')!r}).")
    if (str(run.get("conclusion") or "")).strip().lower() != "success":
        problems.append(f"run conclusion {run.get('conclusion')!r} != 'success'.")
    if spec.expected_head_sha:
        if (str(run.get("head_sha") or "")).strip().lower() != spec.expected_head_sha.lower():
            problems.append(
                f"run head_sha {run.get('head_sha')!r} != expected {spec.expected_head_sha!r}."
            )
    return problems


def _artifact_matches_spec(artifact: dict, spec: EvidenceSpec, run_id: str) -> "list[str]":
    problems: "list[str]" = []
    if (str(artifact.get("name") or "")).strip() != spec.artifact_name:
        problems.append(f"artifact name {artifact.get('name')!r} != expected {spec.artifact_name!r}.")
    wr = artifact.get("workflow_run")
    wr_id = wr.get("id") if isinstance(wr, dict) else None
    if wr_id is None:
        problems.append("artifact metadata does not record its workflow_run id.")
    elif str(wr_id) != str(run_id):
        problems.append(f"artifact belongs to run {wr_id!r}, not the matched run {run_id!r}.")
    if artifact.get("expired") is True:
        problems.append("artifact is expired -- its bytes are no longer retrievable.")
    if _digest_hex(artifact) is None:
        problems.append("artifact has no GitHub-recorded digest.")
    return problems


def _authenticate_bytes(artifact: dict, zip_bytes: bytes) -> "list[str]":
    problems: "list[str]" = []
    digest_hex = _digest_hex(artifact)
    if len(zip_bytes) > ga.MAX_ARTIFACT_ZIP_BYTES:
        problems.append(
            f"downloaded artifact ZIP is {len(zip_bytes)} bytes, over the "
            f"{ga.MAX_ARTIFACT_ZIP_BYTES}-byte limit."
        )
    size = artifact.get("size_in_bytes")
    if isinstance(size, int) and not isinstance(size, bool) and len(zip_bytes) != size:
        problems.append(
            f"ZIP is {len(zip_bytes)} bytes but GitHub records size_in_bytes={size}."
        )
    observed = ga.sha256_hex(zip_bytes)
    if digest_hex is not None and observed != digest_hex:
        problems.append(
            f"ZIP sha256 {observed} != GitHub artifact digest sha256:{digest_hex}."
        )
    return problems


# --- acquisition --------------------------------------------------------------

ProviderCollector = Callable[[EvidenceSpec], dict]


def acquire_release_evidence(
    plan: EvidencePlan,
    *,
    report_root: Path,
    client: "GithubEvidenceClient | None" = None,
    token: "str | None" = None,
    env: "dict[str, str] | None" = None,
    overrides: "dict[str, dict[str, str]] | None" = None,
    provider_collectors: "dict[str, ProviderCollector] | None" = None,
    now: "datetime | None" = None,
    write_manifest: bool = True,
) -> AcquisitionOutcome:
    """Acquire and authenticate every planned evidence item.

    ``overrides`` maps evidence type -> ``{"run_id": ..., "artifact_id": ...,
    "zip_path": ...}`` for diagnostics; overridden identities are verified
    exactly as strictly as discovered ones.
    ``provider_collectors`` maps a platform (``android``/``ios``) to a callable
    returning an already-authenticated provider observation dict.
    """
    outcome = AcquisitionOutcome(plan=plan)
    overrides = overrides or {}
    effective_token = token if token is not None else ga.resolve_token(env)
    needs_github = any(
        s.required and s.repository for s in plan.specs
        if s.evidence_type not in overrides or not overrides[s.evidence_type].get("zip_path")
    )
    if client is None and needs_github:
        if not effective_token:
            missing = " or ".join(ga.TOKEN_ENV_VARS)
            for spec in plan.specs:
                if not spec.required:
                    outcome.items.append(AcquiredItem(
                        spec=spec, status=STATUS_NOT_APPLICABLE,
                        problems=[spec.not_applicable_reason] if spec.not_applicable_reason else [],
                    ))
                elif spec.repository is None:
                    outcome.items.append(_acquire_provider_item(spec, provider_collectors,
                                                                plan, report_root))
                else:
                    outcome.items.append(AcquiredItem(
                        spec=spec, status=STATUS_BLOCKED,
                        problems=[f"no GitHub credentials available (set one of {missing})."],
                        remediation=(
                            f"GitHub authentication missing: set one of {missing} to a token with "
                            f"read access to {spec.repository}, or store it via the approved "
                            f"credential policy. Never pass the token on a command line."
                        ),
                    ))
            _finish(outcome, report_root, write_manifest)
            return outcome
        client = LiveGithubClient(effective_token, now=now)

    acquired_root = evidence_dir(report_root, plan.run_id) / ACQUIRED_DIRNAME
    _clean_interrupted_writes(acquired_root)

    for spec in plan.specs:
        if not spec.required:
            outcome.items.append(AcquiredItem(
                spec=spec, status=STATUS_NOT_APPLICABLE,
                problems=[spec.not_applicable_reason] if spec.not_applicable_reason else [],
            ))
            continue
        if spec.repository is None:
            outcome.items.append(_acquire_provider_item(spec, provider_collectors, plan, report_root))
            continue
        override = overrides.get(spec.evidence_type)
        try:
            item = _acquire_github_item(spec, plan, client, report_root, override=override)
        except AcquisitionError as exc:
            item = AcquiredItem(spec=spec, status=STATUS_BLOCKED, problems=[str(exc)],
                                remediation=str(exc))
        outcome.items.append(item)

    _finish(outcome, report_root, write_manifest)
    return outcome


def _finish(outcome: AcquisitionOutcome, report_root: Path, write_manifest: bool) -> None:
    if not write_manifest:
        return
    manifest_path = evidence_dir(report_root, outcome.plan.run_id) / MANIFEST_FILENAME
    payload = json.dumps(outcome.to_manifest_dict(), indent=2, sort_keys=True).encode("utf-8")
    write_atomic_private(manifest_path, payload)
    outcome.manifest_path = str(manifest_path)


def _remediation_for_zero(spec: EvidenceSpec) -> str:
    if spec.evidence_type == TYPE_SELECTOR_CERTIFICATION:
        return (
            f"selector certification has not been run for this release: dispatch the approved "
            f"{spec.workflow_file} workflow in {spec.repository} for CaleeMobile "
            f"{spec.expected_product_sha} / {spec.expected_version} / release {spec.release_id}."
        )
    return (
        f"no successful merged-main run exists in {spec.repository} ({spec.workflow_file}) for "
        f"SHA {spec.expected_head_sha}: merge the commit to main (or wait for its CI) and rerun."
    )


def _acquire_github_item(
    spec: EvidenceSpec,
    plan: EvidencePlan,
    client: GithubEvidenceClient,
    report_root: Path,
    *,
    override: "dict[str, str] | None" = None,
) -> AcquiredItem:
    if spec.expected_head_sha is None and spec.evidence_type != TYPE_SELECTOR_CERTIFICATION:
        return AcquiredItem(
            spec=spec, status=STATUS_BLOCKED,
            problems=["expected head SHA could not be derived -- refusing to guess."],
            remediation=(
                "the expected framework SHA is unknown: run acquisition from a workspace with a "
                "recorded baseline, or supply a diagnostic override after verifying the identity."
            ),
        )

    if override and (override.get("run_id") or override.get("artifact_id")):
        return _acquire_with_override(spec, plan, client, report_root, override)

    if spec.evidence_type == TYPE_SELECTOR_CERTIFICATION:
        return _acquire_selector(spec, plan, client, report_root)
    return _acquire_main_ci(spec, plan, client, report_root)


def _select_unique_run(spec: EvidenceSpec, client: GithubEvidenceClient) -> "tuple[dict | None, list[str], str | None]":
    """Returns (run, problems, remediation). Exactly one exact match or nothing."""
    candidates: "list[dict]" = []
    for event in spec.approved_events:
        candidates.extend(client.list_workflow_runs(
            spec.repository, spec.workflow_file,
            head_sha=spec.expected_head_sha, event=event,
            branch=spec.required_branch if event == mce.MAIN_EVENT_PUSH else None,
        ))
    seen: "set[str]" = set()
    matches: "list[dict]" = []
    for run in candidates[:MAX_RUN_CANDIDATES]:
        rid = str(run.get("id"))
        if rid in seen:
            continue
        seen.add(rid)
        if not _run_matches_spec(run, spec):
            matches.append(run)
    if not matches:
        return None, [f"no matching successful run found in {spec.repository} for "
                      f"workflow {spec.workflow_file} and SHA {spec.expected_head_sha}."], \
            _remediation_for_zero(spec)
    if len(matches) > 1:
        ids = sorted(str(m.get("id")) for m in matches)
        return None, [f"multiple matching runs are ambiguous ({ids}) -- refusing to pick one."], \
            ("matching workflow runs are ambiguous: identify the correct run and supply it as a "
             "diagnostic override after verifying it, or remove the duplicate runs.")
    return matches[0], [], None


def _select_unique_artifact(spec: EvidenceSpec, client: GithubEvidenceClient,
                            run: dict) -> "tuple[dict | None, list[str], str | None]":
    artifacts = client.list_run_artifacts(spec.repository, str(run.get("id")))
    named = [a for a in artifacts if (str(a.get("name") or "")).strip() == spec.artifact_name]
    if not named:
        return None, [f"run {run.get('id')} has no artifact named {spec.artifact_name!r}."], \
            (f"the matched run produced no {spec.artifact_name!r} artifact -- it may have expired "
             f"or the workflow is not emitting the expected naming contract.")
    if len(named) > 1:
        return None, [f"run {run.get('id')} has multiple artifacts named {spec.artifact_name!r} -- ambiguous."], \
            "matching artifacts are ambiguous: exactly one is required."
    artifact = named[0]
    problems = _artifact_matches_spec(artifact, spec, str(run.get("id")))
    if problems:
        remediation = ("matching artifact is expired: re-run the workflow for this exact SHA."
                       if artifact.get("expired") is True else None)
        return None, problems, remediation
    return artifact, [], None


def _obtain_zip(spec: EvidenceSpec, plan: EvidencePlan, client: GithubEvidenceClient,
                report_root: Path, run: dict, artifact: dict,
                *, local_zip_path: "str | None" = None) -> "tuple[bytes, str, str]":
    """Returns (zip_bytes, cached_path, source) reusing the run-scoped cache
    only after re-authenticating against the freshly fetched GitHub digest."""
    cache = cache_path_for(
        report_root, plan.run_id, evidence_type=spec.evidence_type,
        repository=spec.repository, workflow_run_id=str(run.get("id")),
        artifact_id=str(artifact.get("id")),
    )
    _ensure_inside(evidence_dir(report_root, plan.run_id), cache)
    digest_hex = _digest_hex(artifact)
    if local_zip_path:
        try:
            zip_bytes = Path(local_zip_path).read_bytes()
        except OSError as exc:
            raise AcquisitionError(f"could not read supplied ZIP {local_zip_path}: {exc}") from exc
        write_atomic_private(cache, zip_bytes)
        return zip_bytes, str(cache), SOURCE_EXPLICIT_OVERRIDE
    cached = load_cached_zip(cache, expected_digest_hex=digest_hex)
    if cached is not None:
        return cached, str(cache), SOURCE_CACHE
    zip_bytes = client.download_artifact_zip(spec.repository, str(artifact.get("id")))
    write_atomic_private(cache, zip_bytes)
    return zip_bytes, str(cache), SOURCE_AUTOMATIC


def _acquire_main_ci(spec: EvidenceSpec, plan: EvidencePlan, client: GithubEvidenceClient,
                     report_root: Path) -> AcquiredItem:
    run, problems, remediation = _select_unique_run(spec, client)
    if run is None:
        return AcquiredItem(spec=spec, status=STATUS_BLOCKED, problems=problems,
                            remediation=remediation)
    artifact, problems, remediation = _select_unique_artifact(spec, client, run)
    if artifact is None:
        return AcquiredItem(spec=spec, status=STATUS_BLOCKED, problems=problems,
                            remediation=remediation, run_data=run)
    zip_bytes, cached_path, source = _obtain_zip(spec, plan, client, report_root, run, artifact)
    return _verify_main_ci_bytes(spec, run, artifact, zip_bytes,
                                 cached_path=cached_path, source=source)


def _verify_main_ci_bytes(spec: EvidenceSpec, run: dict, artifact: dict, zip_bytes: bytes,
                          *, cached_path: "str | None", source: str) -> AcquiredItem:
    item = AcquiredItem(
        spec=spec, status=STATUS_BLOCKED, source=source, run_data=run, artifact_data=artifact,
        github_digest=("sha256:" + _digest_hex(artifact)) if _digest_hex(artifact) else None,
        observed_digest="sha256:" + ga.sha256_hex(zip_bytes), cached_path=cached_path,
        verified_at=_utc_now_iso(),
    )
    origin_problems = _authenticate_bytes(artifact, zip_bytes)
    if origin_problems:
        item.problems = origin_problems
        item.verification_result = "origin-authentication-failed"
        return item
    try:
        raw, summary = ga.extract_single_result(zip_bytes, expected_name=spec.result_filename)
    except ga.GithubArtifactError as exc:
        item.problems = [str(exc)]
        item.verification_result = "malformed-artifact"
        return item
    content_problems = mce.verify_main_ci_evidence(
        summary,
        expected_sha=spec.expected_head_sha,
        raw_bytes=raw,
        expected_repository=spec.repository,
        expected_workflow_file=spec.workflow_file,
        canonical_required_gates=(list(spec.canonical_required_gates)
                                  if spec.canonical_required_gates else None),
    )
    item.result = summary
    if content_problems:
        # Origin is fully authenticated (exact repo/workflow/SHA/digest) and
        # the run concluded success, yet the summary contradicts -- that is a
        # genuine evidence contradiction under the result policy (exit 1).
        item.status = STATUS_CONTRADICTED
        item.problems = content_problems
        item.verification_result = "content-contradiction"
        return item
    item.status = STATUS_REUSED_CACHE if source == SOURCE_CACHE else STATUS_ACQUIRED
    item.verification_result = "verified"
    return item


def _acquire_selector(spec: EvidenceSpec, plan: EvidencePlan, client: GithubEvidenceClient,
                      report_root: Path) -> AcquiredItem:
    candidates: "list[dict]" = []
    for event in spec.approved_events:
        candidates.extend(client.list_workflow_runs(spec.repository, spec.workflow_file, event=event))
    runs = [r for r in candidates[:MAX_RUN_CANDIDATES] if not _run_matches_spec(r, spec)]
    matches: "list[AcquiredItem]" = []
    rejected: "list[str]" = []
    for run in runs[:MAX_SELECTOR_DOWNLOAD_CANDIDATES]:
        artifact, problems, _rem = _select_unique_artifact(spec, client, run)
        if artifact is None:
            rejected.append(f"run {run.get('id')}: " + " ".join(problems))
            continue
        zip_bytes, cached_path, source = _obtain_zip(spec, plan, client, report_root, run, artifact)
        item = _verify_selector_bytes(spec, run, artifact, zip_bytes,
                                      cached_path=cached_path, source=source)
        if item.status in (STATUS_ACQUIRED, STATUS_REUSED_CACHE, STATUS_CONTRADICTED):
            matches.append(item)
        else:
            rejected.append(f"run {run.get('id')}: " + " ".join(item.problems))
            _discard_cache(item)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        problems = ["no selector-certification evidence matches the exact release tuple "
                    f"(CaleeMobile {spec.expected_product_sha} / {spec.expected_version} / "
                    f"release {spec.release_id})."] + rejected[:5]
        return AcquiredItem(spec=spec, status=STATUS_BLOCKED, problems=problems,
                            remediation=_remediation_for_zero(spec))
    for extra in matches:
        _discard_cache(extra)
    ids = sorted(str(m.run_data.get("id")) for m in matches if m.run_data)
    return AcquiredItem(
        spec=spec, status=STATUS_BLOCKED,
        problems=[f"multiple selector runs match the exact release tuple ({ids}) -- ambiguous."],
        remediation=("matching artifacts are ambiguous: identify the intended certification run "
                     "and supply it as a diagnostic override after verifying it."),
    )


def _discard_cache(item: AcquiredItem) -> None:
    if item.cached_path and item.source == SOURCE_AUTOMATIC:
        try:
            Path(item.cached_path).unlink(missing_ok=True)
        except OSError:
            pass
        item.cached_path = None


def _verify_selector_bytes(spec: EvidenceSpec, run: dict, artifact: dict, zip_bytes: bytes,
                           *, cached_path: "str | None", source: str) -> AcquiredItem:
    item = AcquiredItem(
        spec=spec, status=STATUS_BLOCKED, source=source, run_data=run, artifact_data=artifact,
        github_digest=("sha256:" + _digest_hex(artifact)) if _digest_hex(artifact) else None,
        observed_digest="sha256:" + ga.sha256_hex(zip_bytes), cached_path=cached_path,
        verified_at=_utc_now_iso(),
    )
    origin_problems = _authenticate_bytes(artifact, zip_bytes)
    if origin_problems:
        item.problems = origin_problems
        item.verification_result = "origin-authentication-failed"
        return item
    try:
        _raw, result = ga.extract_single_result(zip_bytes, expected_name=spec.result_filename)
    except ga.GithubArtifactError as exc:
        item.problems = [str(exc)]
        item.verification_result = "malformed-artifact"
        return item
    item.result = result
    # Only NOW (origin authenticated) is the JSON content consulted.
    problems: "list[str]" = []
    ev_run = result.get("workflowRunId")
    if ev_run is None or str(ev_run) != str(run.get("id")):
        problems.append(f"evidence workflowRunId {ev_run!r} != verified run {run.get('id')!r}.")
    ev_regression = result.get("regressionSha")
    head_sha = str(run.get("head_sha") or "").strip().lower()
    if not isinstance(ev_regression, str) or ev_regression.lower() != head_sha:
        problems.append(f"evidence regressionSha {ev_regression!r} != run head_sha {run.get('head_sha')!r}.")
    try:
        parsed = se.parse_selector_contract_result(result)
    except se.SelectorEvidenceError as exc:
        item.problems = problems + [str(exc)]
        item.verification_result = "malformed-artifact"
        return item
    tuple_mismatch: "list[str]" = []
    if (parsed.tested_sha or "").lower() != (spec.expected_product_sha or "").lower():
        tuple_mismatch.append(f"testedSha {parsed.tested_sha!r} != expected {spec.expected_product_sha!r}.")
    if (parsed.pubspec_version or "") != (spec.expected_version or ""):
        tuple_mismatch.append(f"pubspecVersion {parsed.pubspec_version!r} != expected {spec.expected_version!r}.")
    if (parsed.release_id or None) != spec.release_id:
        tuple_mismatch.append(f"releaseId {parsed.release_id!r} != expected {spec.release_id!r}.")
    if parsed.schema_version not in se.SUPPORTED_SCHEMA_VERSIONS:
        tuple_mismatch.append(f"selector schemaVersion {parsed.schema_version!r} is not supported.")
    if problems or tuple_mismatch:
        item.problems = problems + tuple_mismatch
        item.verification_result = "identity-mismatch"
        return item
    verdict = se.verify_selector_contract_evidence(
        parsed,
        expected_git_sha=spec.expected_product_sha,
        expected_version=spec.expected_version,
        expected_release_id=spec.release_id,
    )
    if not verdict.ok:
        # Exact tuple, authenticated origin, but the certification itself did
        # not hold (contract FAIL / stale) -- a contradiction, not "not found".
        item.status = STATUS_CONTRADICTED
        item.problems = verdict.problems
        item.verification_result = "content-contradiction"
        return item
    item.status = STATUS_REUSED_CACHE if source == SOURCE_CACHE else STATUS_ACQUIRED
    item.verification_result = "verified"
    return item


def _acquire_with_override(spec: EvidenceSpec, plan: EvidencePlan, client: GithubEvidenceClient,
                           report_root: Path, override: "dict[str, str]") -> AcquiredItem:
    run_id = override.get("run_id")
    artifact_id = override.get("artifact_id")
    if not run_id or not artifact_id:
        return AcquiredItem(
            spec=spec, status=STATUS_BLOCKED, source=SOURCE_EXPLICIT_OVERRIDE,
            problems=["a diagnostic override needs BOTH a run id and an artifact id."],
            remediation="supply both --*-run-id and --*-artifact-id, or neither.",
        )
    run = client.get_workflow_run(spec.repository, run_id)
    artifact = client.get_artifact(spec.repository, artifact_id)
    problems = _run_matches_spec(run, spec) + _artifact_matches_spec(artifact, spec, str(run.get("id")))
    if problems:
        return AcquiredItem(spec=spec, status=STATUS_BLOCKED, source=SOURCE_EXPLICIT_OVERRIDE,
                            run_data=run, artifact_data=artifact, problems=problems,
                            remediation="the explicitly supplied run/artifact does not match the "
                                        "expected release identity -- overrides are authenticated "
                                        "exactly like discovered evidence.")
    zip_bytes, cached_path, _source = _obtain_zip(
        spec, plan, client, report_root, run, artifact,
        local_zip_path=override.get("zip_path"),
    )
    if spec.evidence_type == TYPE_SELECTOR_CERTIFICATION:
        item = _verify_selector_bytes(spec, run, artifact, zip_bytes,
                                      cached_path=cached_path, source=SOURCE_EXPLICIT_OVERRIDE)
    else:
        item = _verify_main_ci_bytes(spec, run, artifact, zip_bytes,
                                     cached_path=cached_path, source=SOURCE_EXPLICIT_OVERRIDE)
    item.source = SOURCE_EXPLICIT_OVERRIDE
    return item


def _recorded_distributed_build_item(spec: EvidenceSpec, plan: EvidencePlan,
                                     report_root: Path) -> "AcquiredItem | None":
    """Already-recorded, authenticated distributed-build provenance in THIS
    run's workspace satisfies the item after full re-verification (tier must
    be an authenticated tier; release/run binding re-checked)."""
    from . import distributed_build_provenance as dbp

    report_path = (Path(report_root) / "reports" / "runs" / plan.run_id /
                   "distributed-build-acceptance" / "results.json")
    if not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    record = report.get("provenance") if isinstance(report, dict) else None
    if not isinstance(record, dict):
        return None
    source = dbp.source_evidence_of(record) or {}
    if source.get("platform") != spec.platform:
        return None
    try:
        problems = dbp.verify_provenance_record(
            record,
            expected_release_run_id=plan.run_id,
            expected_release_id=spec.release_id,
            expected_git_sha=spec.expected_product_sha,
            expected_version=spec.expected_version,
        )
    except dbp.DistributedProvenanceError:
        return None
    tier = record.get("evidenceTier")
    if problems or tier not in pe.AUTHENTICATED_TIERS:
        return None
    return AcquiredItem(
        spec=spec, status=STATUS_REUSED_CACHE, source=SOURCE_RECORDED,
        cached_path=str(report_path), verified_at=_utc_now_iso(),
        verification_result="verified",
        observed_digest=record.get("sourceContentDigest"),
    )


def _acquire_provider_item(spec: EvidenceSpec, provider_collectors: "dict[str, ProviderCollector] | None",
                           plan: EvidencePlan, report_root: Path) -> AcquiredItem:
    recorded = _recorded_distributed_build_item(spec, plan, report_root)
    if recorded is not None:
        return recorded
    collector = (provider_collectors or {}).get(spec.platform or "")
    if collector is None:
        store = ("App Store Connect" if spec.platform == pe.PLATFORM_IOS else "Play Console")
        creds = ("CALEE_ASC_KEY_ID / CALEE_ASC_ISSUER_ID / CALEE_ASC_PRIVATE_KEY"
                 if spec.platform == pe.PLATFORM_IOS
                 else "CALEE_PLAY_ACCESS_TOKEN or CALEE_PLAY_SERVICE_ACCOUNT_JSON")
        return AcquiredItem(
            spec=spec, status=STATUS_BLOCKED,
            problems=[f"{store} evidence unavailable: no approved provider collector/credentials "
                      f"are configured for automatic lookup."],
            remediation=(f"{store} evidence unavailable: configure {creds} via the approved "
                         f"credential policy and record distributed-build evidence with "
                         f"record-distributed-build-acceptance, or attach an approved "
                         f"signed-export evidence package. A placeholder PASS is never fabricated."),
        )
    try:
        observation = collector(spec)
    except Exception as exc:  # noqa: BLE001 - any collector fault BLOCKS, never a fabricated pass
        return AcquiredItem(spec=spec, status=STATUS_BLOCKED, problems=[str(exc)],
                            remediation=str(exc))
    problems = pe.validate_provider_observation(observation, expected_release_id=spec.release_id)
    obs_platform = observation.get("platform")
    if obs_platform != spec.platform:
        problems.append(f"provider observation platform {obs_platform!r} != required {spec.platform!r}.")
    raw = json.dumps(observation, indent=2, sort_keys=True).encode("utf-8")
    digest = "sha256:" + ga.sha256_hex(raw)
    item = AcquiredItem(
        spec=spec,
        status=STATUS_BLOCKED if problems else STATUS_ACQUIRED,
        source=SOURCE_AUTOMATIC, problems=problems,
        observed_digest=digest, verified_at=_utc_now_iso(),
        verification_result="verified" if not problems else "identity-mismatch",
        result=observation,
    )
    if not problems:
        path = evidence_dir(report_root, plan.run_id) / ACQUIRED_DIRNAME / (
            f"{_safe_component(spec.evidence_type)}--provider--"
            f"{_safe_component(str(observation.get('providerRecordId') or 'record'))}.json"
        )
        _ensure_inside(evidence_dir(report_root, plan.run_id), path)
        write_atomic_private(path, raw)
        item.cached_path = str(path)
    return item


# --- read-only inspection -------------------------------------------------------


def inspect_release_evidence(
    plan: EvidencePlan,
    *,
    report_root: Path,
    client: "GithubEvidenceClient | None" = None,
    token: "str | None" = None,
    env: "dict[str, str] | None" = None,
    provider_collectors: "dict[str, ProviderCollector] | None" = None,
) -> dict:
    """Read-only planning report: what is expected, what is cached, what needs
    a GitHub lookup, whether credentials exist, how many runs match, and
    whether acquisition can proceed. Downloads nothing, writes nothing."""
    effective_token = token if token is not None else ga.resolve_token(env)
    have_client = client is not None or bool(effective_token)
    if client is None and effective_token:
        client = LiveGithubClient(effective_token)
    manifest_path = evidence_dir(report_root, plan.run_id) / MANIFEST_FILENAME
    cached_manifest: "dict | None" = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cached_manifest = loaded
        except (OSError, json.JSONDecodeError):
            cached_manifest = None

    items: "list[dict]" = []
    can_proceed = True
    for spec in plan.specs:
        entry: "dict[str, Any]" = {"spec": spec.to_dict()}
        if not spec.required:
            entry["assessment"] = "not applicable to this release scope."
            items.append(entry)
            continue
        if spec.repository is None:
            has_collector = bool((provider_collectors or {}).get(spec.platform or ""))
            entry["assessment"] = (
                "distributed-build provider lookup available." if has_collector else
                "distributed-build provider prerequisites missing (no approved collector/credentials)."
            )
            if not has_collector:
                can_proceed = False
            items.append(entry)
            continue
        cached_entry = None
        for prior in (cached_manifest or {}).get("items", []):
            if isinstance(prior, dict) and prior.get("evidenceType") == spec.evidence_type \
                    and prior.get("cachedPath") and Path(str(prior["cachedPath"])).is_file():
                cached_entry = prior
        entry["cached"] = bool(cached_entry)
        if not have_client:
            entry["assessment"] = (
                "requires a GitHub lookup, but no GitHub credentials are available "
                f"(set one of {' or '.join(ga.TOKEN_ENV_VARS)})."
            )
            can_proceed = False
            items.append(entry)
            continue
        if spec.evidence_type == TYPE_SELECTOR_CERTIFICATION:
            entry["assessment"] = "requires a GitHub lookup (selector tuple matched after origin authentication)."
            items.append(entry)
            continue
        try:
            run, problems, _rem = _select_unique_run(spec, client)
        except AcquisitionError as exc:
            entry["assessment"] = f"GitHub lookup failed: {exc}"
            can_proceed = False
            items.append(entry)
            continue
        if run is None:
            entry["assessment"] = "; ".join(problems)
            entry["matchingRuns"] = 0 if problems and problems[0].startswith("no matching") else "ambiguous"
            can_proceed = False
        else:
            entry["matchingRuns"] = 1
            entry["workflowRunId"] = str(run.get("id"))
            entry["assessment"] = "exactly one matching run found."
        items.append(entry)
    return {
        "schemaVersion": ACQUISITION_SCHEMA_VERSION,
        "runId": plan.run_id,
        "releaseId": plan.release_id,
        "credentialsAvailable": have_client,
        "planProblems": list(plan.problems),
        "items": items,
        "canProceed": can_proceed and not plan.problems,
    }
