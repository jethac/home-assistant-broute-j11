"""Diagnostics for the B-route Smart Meter (J11) integration.

Everything that identifies a household or its meter is redacted: credentials,
the meter's MAC address and IPv6 address, the PAN ID, the manufacturing number
and the USB path (which usually embeds the adapter's serial number).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import BrouteConfigEntry

REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BrouteConfigEntry
) -> dict[str, Any]:
    """Return redacted protocol state and counters for ``entry``."""
    coordinator = entry.runtime_data
    session = coordinator.session
    link = session.link
    profile = session.profile
    return {
        "entry": {
            "device": REDACTED,
            "auth_id": REDACTED,
            "password": REDACTED,
            "options": dict(entry.options),
        },
        "session": {
            "connected": session.connected,
            "authentication_failed": session.authentication_failed,
            "channel": link.channel if link is not None else None,
            "pan_id": REDACTED if link is not None else None,
            "mac_address": REDACTED if link is not None else None,
            "address": REDACTED if link is not None else None,
            "cached_network": REDACTED if session.cached_network is not None else None,
            "rssi": link.rssi if link is not None else None,
            "firmware_version": link.firmware_version if link is not None else None,
        },
        "meter": {
            "coefficient": profile.coefficient,
            "unit": str(profile.unit),
            "digits": profile.digits,
            "manufacturer_code": profile.manufacturer_code,
            "standard_version": profile.standard_version,
            "serial_number": REDACTED if profile.serial_number else None,
        },
        "statistics": asdict(session.stats),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval is not None
                else None
            ),
        },
    }
