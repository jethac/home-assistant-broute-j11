"""Tests for config entry setup, unload and reload."""

from __future__ import annotations

from broute_j11.commands import CommandCode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.broute_j11.const import (
    CONF_CHANNEL,
    CONF_MAC_ADDRESS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

from .fixtures.fake_adapter import (
    METER_CHANNEL,
    METER_MAC,
    AdapterBehaviour,
    FakeAdapter,
)


async def test_setup_pairs_and_polls(
    hass: HomeAssistant, adapter: FakeAdapter, setup_integration: MockConfigEntry
) -> None:
    assert setup_integration.state is ConfigEntryState.LOADED
    assert setup_integration.runtime_data.data.instantaneous_power is not None
    assert adapter.opens == 1


async def test_unload_releases_the_serial_port(
    hass: HomeAssistant, adapter: FakeAdapter, setup_integration: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    assert adapter.closes >= 1


async def test_changing_options_reloads_the_entry(
    hass: HomeAssistant, adapter: FakeAdapter, setup_integration: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(
        setup_integration, options={CONF_SCAN_INTERVAL: 90}
    )
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.LOADED
    assert setup_integration.runtime_data.update_interval is not None
    assert setup_integration.runtime_data.update_interval.total_seconds() == 90
    assert adapter.opens == 2


async def test_the_joined_network_is_stored_and_reused(
    hass: HomeAssistant, adapter: FakeAdapter, setup_integration: MockConfigEntry
) -> None:
    assert setup_integration.data[CONF_CHANNEL] == METER_CHANNEL
    assert setup_integration.data[CONF_MAC_ADDRESS] == METER_MAC.hex()
    scans = len(adapter.sent(CommandCode.ACTIVE_SCAN))
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.LOADED
    # The second setup rejoins the cached network instead of sweeping channels.
    assert len(adapter.sent(CommandCode.ACTIVE_SCAN)) == scans


async def test_a_missing_adapter_defers_setup(
    hass: HomeAssistant,
    behaviour: AdapterBehaviour,
    adapter: FakeAdapter,
    config_entry: MockConfigEntry,
) -> None:
    behaviour.fail_open = True
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_credentials_ask_for_reauthentication(
    hass: HomeAssistant,
    behaviour: AdapterBehaviour,
    adapter: FakeAdapter,
    config_entry: MockConfigEntry,
) -> None:
    behaviour.pana_result = 0x02
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["step_id"] for flow in flows] == ["reauth_confirm"]
