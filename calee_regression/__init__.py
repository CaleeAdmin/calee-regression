__version__ = "0.1.0"

# NOTE: Appium UiAutomator2 "Settings app is not running" recovery is NO LONGER
# installed here as an import-time monkey-patch of CaleeDriver.start_session.
# Package import must not implicitly change class behaviour. The recovery now
# lives in the explicit, testable session_bootstrap.bootstrap_session component,
# which the runner calls directly (see calee_regression/session_bootstrap.py).
