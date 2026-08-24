"""Constants for the B-route Smart Meter (J11) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "broute_j11"

CONF_DEVICE: Final = "device"
CONF_AUTH_ID: Final = "auth_id"
CONF_PASSWORD: Final = "password"
CONF_SCAN_INTERVAL: Final = "scan_interval"

#: Polling bounds for the instantaneous properties (PRD §6.5).
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 300

#: Sentinel offered by the config flow when the wanted device is not listed.
MANUAL_PATH: Final = "manual"

MANUFACTURER: Final = "ROHM BP35C0-J11 compatible"
