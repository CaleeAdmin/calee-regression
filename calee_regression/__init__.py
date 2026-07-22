__version__ = "0.1.0"

# Install the narrowly-scoped UiAutomator2 bootstrap recovery before runner.py
# imports CaleeDriver. The patch is idempotent and retries only the exact known
# Appium Settings startup failure; all other errors retain existing behavior.
from .appium_recovery import install_appium_settings_recovery

install_appium_settings_recovery()
