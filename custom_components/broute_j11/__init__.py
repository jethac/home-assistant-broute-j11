"""The B-route Smart Meter (J11) integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import CONF_AUTH_ID, CONF_DEVICE, CONF_PASSWORD
from .coordinator import BrouteConfigEntry, BrouteCoordinator
from .protocol.codec import ProtocolError
from .protocol.session import (
    AuthenticationError,
    J11Session,
    SessionConfig,
    SessionError,
)
from .protocol.transport import SerialTransport, TransportError

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: BrouteConfigEntry) -> bool:
    """Pair with the meter and start polling it."""
    session = J11Session(
        SerialTransport(entry.data[CONF_DEVICE]),
        SessionConfig(
            auth_id=entry.data[CONF_AUTH_ID],
            password=entry.data[CONF_PASSWORD],
        ),
    )
    try:
        await session.async_connect()
    except AuthenticationError as err:
        await session.async_close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except (SessionError, ProtocolError, TransportError) as err:
        await session.async_close()
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = BrouteCoordinator(hass, entry, session)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        await session.async_close()
        raise
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BrouteConfigEntry) -> bool:
    """Unload the entry and release the serial port."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: BrouteConfigEntry) -> None:
    """Reload the entry after its options changed."""
    await hass.config_entries.async_reload(entry.entry_id)
