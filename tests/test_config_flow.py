"""Tests for the config and options flow."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.broute_j11.const import (
    CONF_AUTH_ID,
    CONF_DEVICE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.broute_j11.protocol.commands import NotificationCode

from .conftest import AUTH_ID, DEVICE, PASSWORD
from .fixtures.fake_adapter import AdapterBehaviour, FakeAdapter

USER_INPUT = {
    CONF_DEVICE: DEVICE,
    CONF_AUTH_ID: AUTH_ID,
    CONF_PASSWORD: PASSWORD,
}


def fail_open(behaviour: AdapterBehaviour) -> None:
    """Make the USB device look absent."""
    behaviour.fail_open = True


def reject_pana(behaviour: AdapterBehaviour) -> None:
    """Make the meter refuse the credentials."""
    behaviour.pana_result = 0x02


def hide_meter(behaviour: AdapterBehaviour) -> None:
    """Make the active scan find nothing."""
    behaviour.beacon_channel = None


def swallow_startup(behaviour: AdapterBehaviour) -> None:
    """Make the adapter go silent after the reset request."""
    behaviour.silent_notifications = {NotificationCode.STARTUP_COMPLETED}


async def start_flow(hass: HomeAssistant) -> str:
    """Open the user step and return its flow ID."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_pairing_creates_an_entry(
    hass: HomeAssistant, adapter: FakeAdapter
) -> None:
    flow_id = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == USER_INPUT
    # The credentials are only used to pair; the entry stores no meter identity.
    assert result["result"].unique_id is not None
    assert AUTH_ID not in str(result["result"].unique_id)


async def test_the_same_meter_is_refused_twice(
    hass: HomeAssistant, adapter: FakeAdapter, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    flow_id = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (CONF_AUTH_ID, "TOO-SHORT", "invalid_auth_id"),
        (CONF_AUTH_ID, "Z" * 32, "invalid_auth_id"),
        (CONF_PASSWORD, "short", "invalid_password"),
        (CONF_PASSWORD, "twelve chars", "invalid_password"),
    ],
)
async def test_malformed_credentials_never_reach_the_adapter(
    hass: HomeAssistant,
    adapter: FakeAdapter,
    field: str,
    value: str,
    error: str,
) -> None:
    flow_id = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**USER_INPUT, field: value}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}
    assert adapter.opens == 0


@pytest.mark.parametrize(
    ("break_adapter", "error"),
    [
        (fail_open, "cannot_connect"),
        (reject_pana, "invalid_auth"),
        (hide_meter, "no_meter"),
        (swallow_startup, "timeout"),
    ],
)
async def test_pairing_failures_are_reported(
    hass: HomeAssistant,
    behaviour: AdapterBehaviour,
    adapter: FakeAdapter,
    break_adapter: Callable[[AdapterBehaviour], None],
    error: str,
) -> None:
    break_adapter(behaviour)
    flow_id = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def start_reauth(
    hass: HomeAssistant, behaviour: AdapterBehaviour, entry: MockConfigEntry
) -> str:
    """Fail setup on rejected credentials and return the reauth flow ID."""
    accepted = behaviour.pana_result
    reject_pana(behaviour)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    behaviour.pana_result = accepted
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    assert flow["step_id"] == "reauth_confirm"
    return str(flow["flow_id"])


async def test_reauthentication_replaces_the_credentials(
    hass: HomeAssistant,
    behaviour: AdapterBehaviour,
    adapter: FakeAdapter,
    config_entry: MockConfigEntry,
) -> None:
    flow_id = await start_reauth(hass, behaviour, config_entry)
    new_password = "SyntheticPw2"
    behaviour.password = new_password
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_AUTH_ID: AUTH_ID, CONF_PASSWORD: new_password}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == new_password


async def test_reauthentication_reports_a_second_rejection(
    hass: HomeAssistant,
    behaviour: AdapterBehaviour,
    adapter: FakeAdapter,
    config_entry: MockConfigEntry,
) -> None:
    flow_id = await start_reauth(hass, behaviour, config_entry)
    reject_pana(behaviour)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_AUTH_ID: AUTH_ID, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_the_polling_interval_can_be_changed(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert setup_integration.options[CONF_SCAN_INTERVAL] == 120


async def test_an_out_of_range_interval_is_refused(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    with pytest.raises(Exception, match="scan_interval"):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 5}
        )
