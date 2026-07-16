# Sample consolidated report

Generated with `python -m calee_regression consolidate` from synthetic, hand-written per-framework
JSON (not a real device/backend run) — see `docs/RELEASE_POLICY.md` for the rule it applies and
`framework_tests/test_consolidated_report.py` for the same scenarios exercised as automated tests.

- `consolidated-report.html` — open this one; human-readable.
- `consolidated-report.json` — machine-readable, same content.
- `consolidated-report.junit.xml` — for CI dashboards that ingest JUnit.
- `Calee-Regression-SAMPLE-PASS.zip` — the release bundle (same three files, zipped) that
  `06 Test Full Calee Solution.command` produces for a real run.

This example shows an overall **PASS**: the tablet suite and CaleeMobile API suite both passed
(always mandatory), and both sample manual checks are recorded as passed. The CaleeMobile Android
and iPhone UI suites are shown as `not_run`/optional here because this sample was explicitly
generated with `--android-optional --ios-optional` (a tablet-only release scope, for illustration).
By default — with no `config/release-platforms.yaml` and no `--android-optional`/`--ios-optional`
override — both platforms default to **mandatory**, and a `not_run` mobile UI result would instead
make the overall status BLOCKED; see `docs/RELEASE_POLICY.md` for the full rule and
`framework_tests/test_release_platforms.py` for the tablet-only / tablet+Android / tablet+Android+
iOS scenarios exercised as automated tests.
