import json

from click.testing import CliRunner

from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def test_consolidate_blocks_without_manual_checks(tmp_path):
    tablet = _write(tmp_path, "tablet.json", {
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    api = _write(tmp_path, "api.json", {"counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]})

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "consolidate",
            "--tablet-report", tablet,
            "--mobile-api-report", api,
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "BLOCKED" in result.output


def test_consolidate_passes_when_everything_is_provided_and_clean(tmp_path):
    tablet = _write(tmp_path, "tablet.json", {
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    api = _write(tmp_path, "api.json", {"counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]})
    manual = _write(tmp_path, "manual.json", [
        {"title": "Kiosk escape check", "instruction": "swipe down", "expectedResult": "no shade", "status": "pass"},
    ])

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "consolidate",
            "--tablet-report", tablet,
            "--mobile-api-report", api,
            "--manual-checks", manual,
            "--build-version", "9.9.9",
            # This release doesn't include mobile UI results at all (a
            # tablet-only scope for this test) -- without an explicit
            # opt-out, Android/iOS UI default to mandatory=True and a
            # missing report would correctly BLOCK. See
            # test_release_platforms.py for the platform-driven cases.
            "--android-optional", "--ios-optional",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "PASS" in result.output

    bundles = list((tmp_path / "out").glob("**/*.zip"))
    assert len(bundles) == 1
    assert "9.9.9" in bundles[0].name
    assert bundles[0].name.endswith("-PASS.zip")
