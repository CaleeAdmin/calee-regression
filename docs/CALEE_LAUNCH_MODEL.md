# Why Calee can't be launched like a normal app

## Calee is not a launcher-visible app

Calee's `AndroidManifest.xml` declares `HomeActivity` (`com.viso.calee/.ui.HomeActivity`) with only
this intent-filter:

```xml
<intent-filter>
    <action android:name="com.viso.calee.action.START" />
    <category android:name="android.intent.category.DEFAULT" />
</intent-filter>
```

There is **no** `action.MAIN` / `category.LAUNCHER` intent-filter. That means:

- Calee has no app icon a launcher can show.
- Android's package manager cannot resolve a "launch intent" for `com.viso.calee`.
- Any tool that relies on the normal launcher intent — including Appium's
  `driver.activate_app("com.viso.calee")` — will fail, because there is nothing for it to resolve.

**This is why `driver.activate_app("com.viso.calee")` is wrong for Calee.** `activate_app` asks
Android's package manager for the app's launcher intent and starts that. Calee deliberately has no
such intent. Using it will either throw or silently do nothing useful. The one exception is the
`normal_launcher` launch strategy (see below), which exists only for apps that *do* have a real
launcher activity — never use it as the default for Calee.

## CaleeShell is the real HOME/launcher app

CaleeShell's `LauncherActivity` (`com.viso.caleeshell/.ui.LauncherActivity`) has:

```xml
<intent-filter>
    <action android:name="android.intent.action.MAIN" />
    <category android:name="android.intent.category.HOME" />
    <category android:name="android.intent.category.DEFAULT" />
</intent-filter>
```

`category.HOME` is what makes an app eligible to be set as the device's home screen. In this tablet
deployment, **CaleeShell is set as the launcher**, and it starts Calee explicitly via the custom
action `com.viso.calee.action.START` when appropriate (e.g. after boot, or when returning to the
foreground app). Calee itself never needs, and does not have, launcher visibility.

## The four `launch_strategy` values

Set in your config's `launch_strategy` field. This framework never calls
`driver.activate_app("com.viso.calee")` for any strategy except `normal_launcher`.

### `direct_activity`

Starts `HomeActivity` directly via `adb shell am start`:

```bash
adb shell am start -W -n com.viso.calee/.ui.HomeActivity -a android.intent.action.MAIN -c android.intent.category.DEFAULT
```

This works because `am start -n <component>` targets a specific activity component directly — it
doesn't need a launcher intent-filter, it just needs the activity to be `exported` (which
`HomeActivity` is).

### `start_action`

Starts Calee via its real declared intent-filter action:

```bash
adb shell am start -W -a com.viso.calee.action.START -p com.viso.calee
```

This is the closest simulation of how CaleeShell actually launches Calee in production.

### `calee_shell`

Starts CaleeShell first, then launches Calee via `start_action` — simulating the real
launcher-hands-off-to-app flow:

```bash
adb shell am start -W -n com.viso.caleeshell/.ui.LauncherActivity
adb shell am start -W -a com.viso.calee.action.START -p com.viso.calee
```

### `normal_launcher`

Uses Appium's `driver.activate_app(app_package)` — **only** valid for apps that have a real
`MAIN`/`LAUNCHER` intent-filter. Calee does not. This strategy exists so the framework can also be
pointed at other, normal Android apps if ever needed; it must never be the default for Calee.

## `PUT_ACTIVITY_HERE` is not a real activity

`PUT_ACTIVITY_HERE` is a placeholder that sometimes gets left behind after copy-pasting a config
template. It is not a real Calee activity name. `calee_regression/config.py` rejects any config
field containing this literal string at load time, with a clear error naming the offending field.
The real Calee activities are `.ui.HomeActivity` (home) and `.ui.login.SignUpActivity` (onboarding,
reached only via redirect from `HomeActivity` when there is no Hub session — it is not exported and
should not be launched directly).
