# Sample consolidated report

Generated with `python -m calee_regression consolidate` from synthetic, hand-written per-framework
JSON (not a real device/backend run) — see `docs/RELEASE_POLICY.md` for the rule it applies and
`framework_tests/test_consolidated_report.py` for the same scenarios exercised as automated tests.

- `consolidated-report.html` — open this one; human-readable.
- `consolidated-report.json` — machine-readable, same content.
- `consolidated-report.junit.xml` — for CI dashboards that ingest JUnit.
- `Calee-Regression-SAMPLE-PASS.zip` — the release bundle (same three files, zipped) that
  `05 Test Full Calee Solution.command` produces for a real run.

This example shows an overall **PASS**: the tablet suite and CaleeMobile API suite both passed
(mandatory), the CaleeMobile Android UI suite is BLOCKED (no device connected — but it's optional,
so this alone doesn't block PASS), the iPhone UI suite wasn't run at all (also optional), and both
sample manual checks are recorded as passed (mandatory once any are defined).
