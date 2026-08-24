"""Coordinator that polls one smart meter over a B-route session."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AUTH_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUFACTURER,
)
from .protocol.codec import ProtocolError
from .protocol.session import (
    AuthenticationError,
    J11Session,
    MeterReading,
    SessionError,
)
from .protocol.transport import TransportError

_LOGGER = logging.getLogger(__name__)

#: Length of the hashed meter identifier used in unique IDs.
_IDENTIFIER_LENGTH = 16


def meter_identifier(auth_id: str) -> str:
    """Return a stable, non-reversible identifier for the meter.

    The authentication ID is a secret and the meter's MAC address is a stable
    private identifier, so entity unique IDs are derived from a digest instead
    (PRD §6.4, §6.7).
    """
    return hashlib.sha256(auth_id.encode("ascii")).hexdigest()[:_IDENTIFIER_LENGTH]


type BrouteConfigEntry = ConfigEntry[BrouteCoordinator]


class BrouteCoordinator(DataUpdateCoordinator[MeterReading]):
    """Poll the meter's instantaneous and cumulative properties."""

    def __init__(
        self, hass: HomeAssistant, entry: BrouteConfigEntry, session: J11Session
    ) -> None:
        """Create a coordinator for ``session``."""
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.session = session
        self.identifier = meter_identifier(entry.data[CONF_AUTH_ID])

    @property
    def device_info(self) -> DeviceInfo:
        """Describe the single device every entity belongs to."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.identifier)},
            manufacturer=MANUFACTURER,
            model="Route-B smart electricity meter",
            name="Smart meter",
            sw_version=self.session.link.firmware_version
            if self.session.link is not None
            else None,
        )

    async def _async_update_data(self) -> MeterReading:
        """Read the meter, letting Home Assistant classify the failure."""
        try:
            return await self.session.async_read_meter()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SessionError, ProtocolError, TransportError) as err:
            raise UpdateFailed(str(err)) from err

    async def async_shutdown(self) -> None:
        """Release the serial port when the entry unloads."""
        await super().async_shutdown()
        await self.session.async_close()
