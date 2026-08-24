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

#: Length of the hashed meter identifier used as the config entry unique ID.
_IDENTIFIER_LENGTH = 16


def meter_identifier(mac_address: bytes) -> str:
    """Return a stable, non-reversible identifier for the meter.

    The meter's MAC address is a stable private identifier, so it is hashed
    before it becomes a config entry unique ID (PRD §6.4, §6.7). Unlike the
    credentials, it survives a credential change, so reauthentication does not
    move the entry to a new identity.
    """
    return hashlib.sha256(mac_address).hexdigest()[:_IDENTIFIER_LENGTH]


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
        # Entity and device identities are the hashed meter MAC, so they survive
        # both a credential correction and removing and re-adding the meter,
        # keeping the Energy dashboard's statistics history attached. The entry
        # ID is only a fallback for an entry created before pairing set one.
        self.identifier = entry.unique_id or entry.entry_id

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
