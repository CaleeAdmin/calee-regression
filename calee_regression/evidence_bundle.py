"""Portable, sanitized evidence bundles (Workstream 3).

A physical run happens on Yiwen's Mac; framework analysis may happen in a
different (cloud) environment. This module exports a run's evidence into a
self-describing, integrity-checked, **secret-free** zip that can be moved
between environments, and verifies/inspects such a bundle entirely offline.

Security model (fail-closed):
  * Only an allowlist of report/diagnostic file TYPES is ever included.
  * Symlinks and any path that escapes the run workspace are rejected.
  * Every textual file is scanned for known credential shapes BEFORE export; a
    match aborts the export (we refuse to prove safety we cannot prove).
  * No password / token / cookie / Authorization header / OAuth code / refresh
    token / CalDAV app password / private key / environment dump / Keychain
    value is ever written.
  * Every included file carries a SHA-256 digest; verification recomputes and
    compares them, and rejects any smuggled entry not named in the manifest.

Two profiles:
  * ``audit`` -- safe to share with a cloud analysis session. Device identifiers
    are pseudonymized; the bundle is marked NON-CERTIFYING after import, so it
    can never promote a scenario or satisfy a release gate.
  * ``local-certification-transfer`` -- preserves the EXACT source reports and
    identities and their recorded eligibility. Digest integrity is verified, but
    a hash alone does NOT prove the exporter's identity unless an authenticated
    signing mechanism exists (documented, never claimed).

Verification/inspection work offline and NEVER populate a live run directory or
overwrite local evidence -- they only read the zip.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from pathlib import Path

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_TYPE = "calee-evidence-bundle"
MANIFEST_NAME = "manifest.json"
EVIDENCE_PREFIX = "evidence/"

PROFILE_AUDIT = "audit"
PROFILE_CERT_TRANSFER = "local-certification-transfer"
PROFILES = (PROFILE_AUDIT, PROFILE_CERT_TRANSFER)

# Allowlisted evidence file types (report + diagnostic). Anything else is
# skipped and recorded, never smuggled in.
_ALLOWED_SUFFIXES = (".json", ".txt", ".xml", ".html", ".junit.xml")
_DENIED_SUFFIXES = (".log", ".env", ".key", ".pem", ".p12", ".mobileprovision", ".keychain")

# Known credential shapes. Matching any of these in a textual evidence file is
# treated as an unsafe export and fails closed. Kept broad on purpose.
_CREDENTIAL_PATTERNS = [
    re.compile(r'"?(pass(word|wd)|secret|token|api[_-]?key|refresh[_-]?token|access[_-]?token|client[_-]?secret)"?\s*[:=]\s*"?[^"\s,}]{4,}', re.I),
    re.compile(r"authorization\s*:\s*\S+", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I),
    re.compile(r"\bset-cookie\s*:", re.I),
    re.compile(r"[?&](code|access_token|id_token|refresh_token)=[^&\s\"']+", re.I),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r'"?caldav[_-]?(app[_-]?)?password"?\s*[:=]', re.I),
]


class EvidenceBundleError(Exception):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_allowlisted(name: str) -> bool:
    low = name.lower()
    if any(low.endswith(s) for s in _DENIED_SUFFIXES):
        return False
    return any(low.endswith(s) for s in _ALLOWED_SUFFIXES)


def _is_textual(name: str) -> bool:
    return name.lower().endswith((".json", ".txt", ".xml", ".html"))


def scan_for_credentials(text: str) -> "list[str]":
    """Return the NAMES of credential shapes found (never the matched value)."""
    hits = []
    for pat in _CREDENTIAL_PATTERNS:
        if pat.search(text):
            hits.append(pat.pattern[:40])
    return hits


def _safe_relpath(base: Path, path: Path) -> str:
    """Relative POSIX path of ``path`` under ``base``; raises on traversal."""
    rel = path.resolve().relative_to(base.resolve())  # raises ValueError if outside
    return rel.as_posix()


# ── export ──────────────────────────────────────────────────────────────────
def _collect_files(run_dir: Path) -> "tuple[list[Path], list[str]]":
    included, skipped = [], []
    for path in sorted(run_dir.rglob("*")):
        if path.is_symlink():
            raise EvidenceBundleError(f"refusing to export a symlink: {path.relative_to(run_dir)}")
        if not path.is_file():
            continue
        rel = _safe_relpath(run_dir, path)  # rejects traversal
        if _is_allowlisted(path.name):
            included.append(path)
        else:
            skipped.append(rel)
    return included, skipped


def _report_meta(text: str) -> dict:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return obj


def _device_ids(reports: "list[dict]") -> "list[str]":
    ids = []
    for r in reports:
        did = r.get("deviceId") or (r.get("provenance") or {}).get("deviceId")
        if did and did not in ids:
            ids.append(did)
    return ids


def export_bundle(
    run_dir: "Path | str",
    output_path: "Path | str",
    *,
    profile: str = PROFILE_AUDIT,
    timestamp: str,
    producer_repo_sha: "str | None" = None,
    product_shas: "dict | None" = None,
) -> dict:
    """Export ``run_dir`` (a reports/runs/<run-id> workspace) into a sanitized,
    integrity-checked zip at ``output_path``. Returns the manifest. Fails closed
    on a symlink, a path escape, or a credential shape in any textual file."""
    if profile not in PROFILES:
        raise EvidenceBundleError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise EvidenceBundleError(f"run workspace not found: {run_dir}")

    included, skipped = _collect_files(run_dir)
    reports = []
    file_entries = []
    pseudonym = {}

    # First pass: read + scan + gather report metadata + device ids.
    raw = {}
    for path in included:
        rel = _safe_relpath(run_dir, path)
        data = path.read_bytes()
        if _is_textual(path.name):
            text = data.decode("utf-8", errors="replace")
            hits = scan_for_credentials(text)
            if hits:
                raise EvidenceBundleError(
                    f"refusing to export {rel}: it matches credential shape(s) {hits}. "
                    "Reports must be secret-free; fix the producer, do not export a secret."
                )
            meta = _report_meta(text) if path.name.endswith(".json") else {}
            if meta:
                reports.append(meta)
        raw[rel] = data

    if profile == PROFILE_AUDIT:
        for i, did in enumerate(_device_ids(reports), 1):
            pseudonym[did] = f"device-{i}"

    # Second pass: (audit) pseudonymize device ids in textual content, digest.
    for path in included:
        rel = _safe_relpath(run_dir, path)
        data = raw[rel]
        if profile == PROFILE_AUDIT and pseudonym and _is_textual(path.name):
            text = data.decode("utf-8", errors="replace")
            for real, fake in pseudonym.items():
                text = text.replace(real, fake)
            data = text.encode("utf-8")
        meta = _report_meta(data.decode("utf-8", errors="replace")) if path.name.endswith(".json") else {}
        file_entries.append({
            "path": rel,
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
            "reportType": meta.get("reportType"),
            "reportSchemaVersion": meta.get("reportSchemaVersion"),
            "certificationEligible": meta.get("certificationEligible"),
            "status": meta.get("status"),
        })
        raw[rel] = data  # possibly-transformed bytes to write

    overall = _overall_identity(reports)
    manifest = {
        "schemaVersion": BUNDLE_SCHEMA_VERSION,
        "bundleType": BUNDLE_TYPE,
        "profile": profile,
        "sourceRunId": run_dir.name,
        "releaseId": overall["releaseId"],
        "producerRepoSha": producer_repo_sha,
        "productShas": product_shas or {},
        "fixtureVersion": overall["fixtureVersion"],
        "backendIdentity": overall["backendIdentity"],
        "featureScope": overall["featureScope"],
        "platformScope": overall["platformScope"],
        "certificationEligibility": overall["certificationEligibility"],
        "exportTimestamp": timestamp,
        "redactionProfile": profile,
        "deviceIdPseudonyms": {v: "pseudonymized" for v in pseudonym.values()} if profile == PROFILE_AUDIT else {},
        "nonCertifyingAfterImport": profile == PROFILE_AUDIT,
        "integrityNote": (
            "SHA-256 digests prove the bundle's INTEGRITY (bytes unchanged). They do NOT prove the "
            "exporter's IDENTITY/authenticity unless an authenticated signing mechanism is added."
        ),
        "skippedFiles": skipped,
        "files": file_entries,
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
        for entry in file_entries:
            zf.writestr(EVIDENCE_PREFIX + entry["path"], raw[entry["path"]])
    return manifest


def _overall_identity(reports: "list[dict]") -> dict:
    release_id = fixture = backend = None
    feature_scope = platform_scope = None
    eligibility = {}
    for r in reports:
        release_id = release_id or r.get("releaseRunId") or r.get("releaseId")
        fixture = fixture or r.get("fixtureVersion")
        backend = backend or r.get("targetEnvironment") or r.get("backend")
        feature_scope = feature_scope or r.get("featureScope")
        platform_scope = platform_scope or r.get("platformScope")
        key = r.get("completenessKey")
        if key:
            eligibility[key] = bool(r.get("certificationEligible"))
    return {
        "releaseId": release_id,
        "fixtureVersion": fixture,
        "backendIdentity": backend,  # a hostname/URL, never a secret
        "featureScope": feature_scope,
        "platformScope": platform_scope,
        "certificationEligibility": eligibility,
    }


# ── verify (offline) ────────────────────────────────────────────────────────
def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        return json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except KeyError as exc:
        raise EvidenceBundleError("bundle has no manifest.json") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise EvidenceBundleError(f"bundle manifest is not valid JSON: {exc}") from exc


def verify_bundle(zip_path: "Path | str") -> dict:
    """Verify a bundle entirely offline: manifest shape, per-file digest
    integrity, no path traversal, no smuggled entries, and (defence in depth) no
    credential shapes. Returns ``{"valid": bool, "problems": [...], "summary": {...}}``.
    Never extracts to disk."""
    zip_path = Path(zip_path)
    problems: "list[str]" = []
    if not zipfile.is_zipfile(zip_path):
        return {"valid": False, "problems": ["not a zip file"], "summary": {}}

    with zipfile.ZipFile(zip_path) as zf:
        # Path-traversal / absolute-path guard on EVERY entry.
        for name in zf.namelist():
            if name.startswith("/") or posixpath.normpath(name).startswith(".."):
                problems.append(f"unsafe zip entry (path traversal): {name}")
        try:
            manifest = _read_manifest(zf)
        except EvidenceBundleError as exc:
            return {"valid": False, "problems": [str(exc)], "summary": {}}

        for field in ("schemaVersion", "bundleType", "profile", "sourceRunId", "files"):
            if field not in manifest:
                problems.append(f"manifest missing required field: {field}")
        if manifest.get("bundleType") != BUNDLE_TYPE:
            problems.append(f"unexpected bundleType {manifest.get('bundleType')!r}")
        if manifest.get("schemaVersion") != BUNDLE_SCHEMA_VERSION:
            problems.append(f"unsupported schemaVersion {manifest.get('schemaVersion')!r}")
        if manifest.get("profile") not in PROFILES:
            problems.append(f"unknown profile {manifest.get('profile')!r}")

        listed = {e["path"] for e in manifest.get("files", [])}
        present = {n[len(EVIDENCE_PREFIX):] for n in zf.namelist() if n.startswith(EVIDENCE_PREFIX)}
        for smuggled in sorted(present - listed):
            problems.append(f"zip contains an evidence file not in the manifest: {smuggled}")
        for missing in sorted(listed - present):
            problems.append(f"manifest lists a file missing from the zip: {missing}")

        for entry in manifest.get("files", []):
            arc = EVIDENCE_PREFIX + entry["path"]
            try:
                data = zf.read(arc)
            except KeyError:
                continue  # already reported as missing
            actual = _sha256_bytes(data)
            if actual != entry.get("sha256"):
                problems.append(f"digest mismatch for {entry['path']} (tampered or corrupt)")
            if _is_textual(entry["path"]):
                hits = scan_for_credentials(data.decode("utf-8", errors="replace"))
                if hits:
                    problems.append(f"credential shape in {entry['path']}: {hits}")

    return {"valid": not problems, "problems": problems, "summary": _summary(manifest)}


def inspect_bundle(zip_path: "Path | str") -> dict:
    """Read-only summary of a bundle -- manifest identity, profile, eligibility
    and file list -- without extracting anything or touching a live run dir."""
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise EvidenceBundleError(f"not a zip file: {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        manifest = _read_manifest(zf)
    summary = _summary(manifest)
    summary["files"] = [{"path": e["path"], "reportType": e.get("reportType"), "sha256": e.get("sha256")}
                        for e in manifest.get("files", [])]
    summary["skippedFiles"] = manifest.get("skippedFiles", [])
    summary["integrityNote"] = manifest.get("integrityNote")
    return summary


def _summary(manifest: dict) -> dict:
    return {
        "bundleType": manifest.get("bundleType"),
        "schemaVersion": manifest.get("schemaVersion"),
        "profile": manifest.get("profile"),
        "sourceRunId": manifest.get("sourceRunId"),
        "releaseId": manifest.get("releaseId"),
        "producerRepoSha": manifest.get("producerRepoSha"),
        "productShas": manifest.get("productShas", {}),
        "fixtureVersion": manifest.get("fixtureVersion"),
        "certificationEligibility": manifest.get("certificationEligibility", {}),
        "nonCertifyingAfterImport": manifest.get("nonCertifyingAfterImport"),
        "fileCount": len(manifest.get("files", [])),
        "exportTimestamp": manifest.get("exportTimestamp"),
    }
