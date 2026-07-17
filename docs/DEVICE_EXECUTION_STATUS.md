# Device-execution status (Workstreams 4–11)

_Recorded: 2026-07-17, from the regression session environment._

Workstreams 4–11 require real hardware/toolchains that are **not present** in this session's
environment. Per the release policy ("do not claim physical execution when no real device or
simulator was available — record it as BLOCKED with the exact missing prerequisite"), each is
recorded **BLOCKED** below with the exact missing prerequisite. None of these is an inferred PASS.

## Environment capability probe

Session host: `Linux x86_64` VM. Mobile/device toolchain availability, probed this session:

| Tool | Present? |
|---|---|
| `adb` (Android Platform Tools) | **No** |
| `flutter` | **No** |
| `appium` | **No** |
| Android `emulator` | **No** |
| `xcodebuild` / macOS | **No** (host is Linux, not macOS) |

With none of these present — and no physical Calee tablet, Android device, iPhone, or CaleeShell
kiosk attached — no product UI/tablet/sync/kiosk execution can run here. The framework logic for
these flows is unit-tested with fakes (see `framework_tests/`), independent of device availability;
what is blocked is the **real-device execution** each workstream ultimately requires.

## Per-workstream status

| WS | Area | Status | Exact missing prerequisite |
|---|---|---|---|
| 4 | Tablet mutation coverage (resolve `UNCONFIRMED_*` selectors) | **BLOCKED** | A real Calee tablet source, or a physical tablet/emulator with Appium Inspector, to confirm the calendar/tasks/chores mutation resource ids. See `docs/TABLET_MUTATION_COVERAGE_GAPS.md`. No tablet/adb/Appium here. |
| 5 | Cross-device synchronization (real flows) | **BLOCKED** | WS4 tablet mutation confirmed, **plus** a live backend + a paired tablet and mobile device/emulator + Appium + flutter. The orchestration is wired and gating (Workstream 1); the real flows can't run without devices. |
| 6 | Real Android suite | **BLOCKED** | An Android emulator or phone + `flutter` + `adb`. None present. |
| 7 | Real iOS suite | **BLOCKED** | A **Mac with Xcode** and an iOS simulator/device. Host is Linux — iOS cannot run here at all. |
| 8 | Deterministic Meals fixture | **BLOCKED** (design/plumbing done, execution blocked) | The release feature profile now carries `meals` (mandatory/optional) and reaches the launcher; a Meals-capable backend + a mobile device are needed to add/verify the run-owned meal record. |
| 9 | Onboarding lifecycle | **BLOCKED** | A disposable redeem/activation-code issuing backend + a display + a mobile device, to exercise account creation, display association, and the one-time mobile handoff. |
| 10 | App-owned OAuth boundaries | **BLOCKED** | The CaleeMobile Dart test harness (`flutter`) to exercise the app-owned authorization-URL/state/callback logic, plus one guided manual real-Google check. No flutter here. |
| 11 | Kiosk & admin physical coverage | **BLOCKED** | A **disposable physical tablet** running CaleeShell with device-owner/admin, to drive the admin-entry gesture, PIN, system-escape and recovery checks. Must never be run on a customer/production tablet. |

## What IS complete without devices (this session)

- **Workstream 1** (sync integration + gating): done and tested — sync is invoked by the full
  launcher, auto-discovered, and release-gating. Because WS4/WS5 are blocked, a mandatory sync
  currently resolves to `BLOCKED`, which correctly prevents a release PASS (the intended safety
  property). See `docs/SUITE_REFERENCE.md` and `docs/RELEASE_POLICY.md`.
- **Workstream 2** (release scope): tablet scope made consistent across execution/consolidation/
  identity; `release_features` profile (synchronization/meals/onboarding/google_calendar/
  kiosk_admin) added and reaching the launcher + consolidation + run manifest.
- **Workstream 3** (intended release identity): production release profile now requires the expected
  identity up front and audits a named dirty-tree waiver. Branch-protection gap reported in
  `docs/BRANCH_PROTECTION_STATUS.md`.

When a real device/tablet/Mac becomes available, run the non-technical launchers
(`01 Prepare Test Environment` → `06 Test Full Calee Solution` → `07 Open Latest Report`); the
consolidated bundle will then include real execution results for the blocked workstreams above, and
any that genuinely pass will flip from BLOCKED to PASS with no further wiring.
