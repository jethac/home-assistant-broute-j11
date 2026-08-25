"""Tests for the meter sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.broute_j11.protocol.echonet import Epc

from . import conftest
from .fixtures import fake_adapter as fake
from .fixtures.fake_adapter import AdapterBehaviour, FakeAdapter

POWER = "sensor.smart_meter_instantaneous_power"
FORWARD = "sensor.smart_meter_cumulative_energy_consumed"
REVERSE = "sensor.smart_meter_cumulative_energy_returned"


async def test_the_meter_readings_are_exposed(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    power = hass.states.get(POWER)
    assert power is not None
    assert power.state == str(fake.EXPECTED_POWER)
    assert power.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.POWER
    assert power.attributes[ATTR_UNIT_OF_MEASUREMENT] == "W"

    forward = hass.states.get(FORWARD)
    assert forward is not None
    assert forward.state == str(fake.EXPECTED_FORWARD_KWH)
    assert forward.attributes["state_class"] is SensorStateClass.TOTAL_INCREASING

    reverse = hass.states.get(REVERSE)
    assert reverse is not None
    assert reverse.state == str(fake.EXPECTED_REVERSE_KWH)


async def test_unique_ids_do_not_leak_the_credentials(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    entry = registry.async_get(POWER)
    assert entry is not None
    assert conftest.AUTH_ID not in entry.unique_id
    assert conftest.PASSWORD not in entry.unique_id
    assert entry.unique_id.endswith("_instantaneous_power")


async def test_a_property_the_meter_omits_stays_unavailable(
    hass: HomeAssistant,
    behaviour: AdapterBehaviour,
    adapter: FakeAdapter,
    config_entry: MockConfigEntry,
) -> None:
    behaviour.properties[Epc.CUMULATIVE_REVERSE_ENERGY] = (0xFFFFFFFE).to_bytes(
        4, "big"
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    reverse = hass.states.get(REVERSE)
    assert reverse is not None
    assert reverse.state == STATE_UNAVAILABLE
    assert hass.states.get(POWER) is not None


async def test_losing_the_meter_marks_the_sensors_unavailable(
    hass: HomeAssistant, adapter: FakeAdapter, setup_integration: MockConfigEntry
) -> None:
    coordinator = setup_integration.runtime_data
    adapter.behaviour.silent_commands = {0x0008}
    adapter.behaviour.beacon_channel = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    power = hass.states.get(POWER)
    assert power is not None
    assert power.state == STATE_UNAVAILABLE
