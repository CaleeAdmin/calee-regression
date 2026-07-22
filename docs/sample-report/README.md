# Sample consolidated report

Generated with `python -m calee_regression consolidate` from synthetic, hand-written per-framework
JSON (not a real device/backend run) — see `docs/RELEASE_POLICY.md` for the rule it applies and
`framework_tests/test_consolidated_report.py` for the same scenarios exercised as automated tests.

- `consolidated-report.html` — open this one; human-readable.
- `consolidated-report.json` — machine-readable, same content.
- `consolidated-report.junit.xml` — for CI dashboards that ingest JUnit.
- `Calee-Regression-SAMPLE-PASS.zip` — the release bundle (same three files, zipped) that
  `06 Test Full Calee Solution.command` produces for a real run.
- `resumed-consolidated-report.json` — the same consolidated JSON shape, but for a run that was
  resumed once (`python -m calee_regression resume-release`) after Prepare originally BLOCKED. Every
  component carries a `"resume"` block: `"Calee tablet release installation"` and `"Release
  configuration ..."` show `"executionMode": "reused"` (a passed installation was reused without
  reinstalling/rebooting the tablet, after a bounded read-only recheck confirmed the same physical
  device); `"Test environment and regression fixture"` shows `"executionMode": "executed"` (Prepare
  was rerun after its original BLOCKED attempt); everything else shows either `"executed"` (it ran
  fresh this attempt) or `"required"` (still not yet run). See `docs/RELEASE_POLICY.md`'s "Resuming a
  blocked run" for the full policy and `framework_tests/test_resume_release.py`/
  `test_cli_resume_release.py` for the same scenario exercised as an automated test.

This example shows an overall **PASS**: the tablet suite and CaleeMobile API suite both passed
(always mandatory), and both sample manual checks are recorded as passed. The CaleeMobile Android
and iPhone UI suites are shown as `not_run`/optional here because this sample was explicitly
generated with `--android-optional --ios-optional` (a tablet-only release scope, for illustration).
By default — with no `config/release-platforms.yaml` and no `--android-optional`/`--ios-optional`
override — both platforms default to **mandatory**, and a `not_run` mobile UI result would instead
make the overall status BLOCKED; see `docs/RELEASE_POLICY.md` for the full rule and
`framework_tests/test_release_platforms.py` for the tablet-only / tablet+Android / tablet+Android+
iOS scenarios exercised as automated tests.
