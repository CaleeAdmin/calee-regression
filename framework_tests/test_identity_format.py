"""Format validation for expected build identities (Workstreams 1 & 2).

Rejects the two ways a configured expected identity can be *malformed* (as
opposed to merely not matching a detected build): an abbreviated Git SHA, and
a version string that isn't a recognisable version. A malformed expectation can
never be safely matched, so it must be rejected up front rather than silently
"not matching" later.
"""

from __future__ import annotations

from calee_regression import release_platforms
from calee_regression.identity_format import is_full_git_sha, is_wellformed_version, split_marketing_version_and_build_number
from calee_regression.release_platforms import ExpectedBuildIdentity

FULL_SHA = "a" * 40


def test_is_full_git_sha_requires_40_hex():
    assert is_full_git_sha(FULL_SHA)
    assert is_full_git_sha("0123456789abcdef0123456789ABCDEF01234567")
    assert not is_full_git_sha("abc1234")          # abbreviated
    assert not is_full_git_sha("g" * 40)           # not hex
    assert not is_full_git_sha("a" * 39)           # too short
    assert not is_full_git_sha("a" * 41)           # too long
    assert not is_full_git_sha(None)
    assert not is_full_git_sha("")
    assert not is_full_git_sha("   ")


def test_is_wellformed_version_accepts_real_calee_versions():
    # The exact shapes used across the Calee solution.
    assert is_wellformed_version("0.0.23+23")       # CaleeMobile pubspec
    assert is_wellformed_version("founder-v0.3.24")  # Calee tablet versionName
    assert is_wellformed_version("founder-v0.2.11")  # CaleeShell versionName
    assert is_wellformed_version("0.3.24")           # bare numeric
    assert is_wellformed_version("1.2.3")


def test_is_wellformed_version_rejects_malformed():
    for bad in ("", "   ", None, "latest", "0.3", "v1.2.3", "1.2.3.4", "0.0.22+", "founder-v0.3", "abc"):
        assert not is_wellformed_version(bad), bad


# --- split_marketing_version_and_build_number (Priority 2, this session) ----


def test_split_marketing_version_and_build_number_splits_caleemobile_shape():
    assert split_marketing_version_and_build_number("0.0.24+24") == ("0.0.24", "24")


def test_split_marketing_version_and_build_number_rejects_no_build_number():
    # A bare "0.3.24" is a WELL-FORMED version (is_wellformed_version accepts
    # it) but has no build number to split off -- ambiguous, never a guess.
    assert split_marketing_version_and_build_number("0.3.24") is None


def test_split_marketing_version_and_build_number_rejects_multiple_plus_signs():
    assert split_marketing_version_and_build_number("0.0.24+24+7") is None


def test_split_marketing_version_and_build_number_rejects_malformed_version():
    for bad in ("", "   ", None, "latest", "0.0.22+", "abc"):
        assert split_marketing_version_and_build_number(bad) is None, bad


# --- validate_expected_build_identity (config-level, Workstream 2) -----------


def test_validate_accepts_a_fully_wellformed_identity():
    identity = ExpectedBuildIdentity(
        caleemobile_git_sha=FULL_SHA,
        calee_git_sha="b" * 40,
        caleemobile_build_version="0.0.23+23",
        calee_build_version="founder-v0.3.24",
        caleeshell_version="founder-v0.2.11",
    )
    assert release_platforms.validate_expected_build_identity(identity) == []


def test_validate_none_fields_are_not_problems():
    # Absent expectations are "no expectation", never a malformed one.
    assert release_platforms.validate_expected_build_identity(ExpectedBuildIdentity()) == []


def test_validate_rejects_abbreviated_shas():
    problems = release_platforms.validate_expected_build_identity(
        ExpectedBuildIdentity(caleemobile_git_sha="abc1234", calee_git_sha="deadbeef")
    )
    joined = " ".join(problems)
    assert "caleemobile_git_sha" in joined
    assert "calee_git_sha" in joined
    assert "abbreviated" in joined.lower()


def test_validate_rejects_malformed_versions():
    problems = release_platforms.validate_expected_build_identity(
        ExpectedBuildIdentity(
            caleemobile_build_version="latest",
            calee_build_version="0.3",
            caleeshell_version="v1",
        )
    )
    joined = " ".join(problems)
    assert "caleemobile_build_version" in joined
    assert "calee_build_version" in joined
    assert "caleeshell_version" in joined
    assert "well-formed" in joined
