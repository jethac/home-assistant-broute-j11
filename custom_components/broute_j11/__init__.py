"""The B-route Smart Meter (J11) integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from broute_j11 import (
    AuthenticationError,
    CachedNetwork,
    J11Session,
    ProtocolError,
    SerialTransport,
    SessionConfig,
    SessionError,
    TransportError,
)

from .const import (
    CONF_AUTH_ID,
    CONF_CHANNEL,
    CONF_DEVICE,
    CONF_MAC_ADDRESS,
    CONF_PAN_ID,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import BrouteConfigEntry, BrouteCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


class EntryNetworkCache:
    """Keeps the joined Route-B network in the config entry (PRD §6.2)."""

    def __init__(self, hass: HomeAssistant, entry: BrouteConfigEntry) -> None:
        """Cache the network of ``entry``."""
        self._hass = hass
        self._entry = entry

    def load(self) -> CachedNetwork | None:
        """Return the network this entry last joined, if it is known."""
        channel = self._entry.data.get(CONF_CHANNEL)
        pan_id = self._entry.data.get(CONF_PAN_ID)
        mac_address = self._entry.data.get(CONF_MAC_ADDRESS)
        if channel is None or pan_id is None or mac_address is None:
            return None
        return CachedNetwork(
            channel=int(channel),
            pan_id=int(pan_id),
            mac_address=bytes.fromhex(str(mac_address)),
        )

    def store(self, network: CachedNetwork) -> None:
        """Persist ``network`` so the next setup can skip the active scan."""
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_CHANNEL: network.channel,
                CONF_PAN_ID: network.pan_id,
                CONF_MAC_ADDRESS: network.mac_address.hex(),
            },
        )


async def async_setup_entry(hass: HomeAssistant, entry: BrouteConfigEntry) -> bool:
    """Pair with the meter and start polling it."""
    session = J11Session(
        SerialTransport(entry.data[CONF_DEVICE]),
        SessionConfig(
            auth_id=entry.data[CONF_AUTH_ID],
            password=entry.data[CONF_PASSWORD],
        ),
        network_cache=EntryNetworkCache(hass, entry),
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
    """Reload the entry when its polling interval changed.

    Caching the joined Route-B network also updates the entry, and that must
    not restart the session that just came up.
    """
    interval = timedelta(
        seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    if interval != entry.runtime_data.update_interval:
        await hass.config_entries.async_reload(entry.entry_id)
