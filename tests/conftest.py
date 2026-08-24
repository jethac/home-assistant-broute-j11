"""Shared fixtures for the Home Assistant tests.

The integration is exercised against the in-memory adapter used by the
protocol tests, so setup, entities and diagnostics run the production
serial-free code path end to end.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.broute_j11.const import (
    CONF_AUTH_ID,
    CONF_DEVICE,
    CONF_PASSWORD,
    DOMAIN,
)
from custom_components.broute_j11.coordinator import meter_identifier
from custom_components.broute_j11.protocol.session import SessionConfig

from .fixtures.fake_adapter import AdapterBehaviour, FakeAdapter

AUTH_ID = "0000000000000000000000000000ABCD"
PASSWORD = "SyntheticPw1"
DEVICE = "/dev/serial/by-id/usb-synthetic-adapter"

ENTRY_DATA = {
    CONF_DEVICE: DEVICE,
    CONF_AUTH_ID: AUTH_ID,
    CONF_PASSWORD: PASSWORD,
}

#: Timeouts short enough that failure paths do not stall the test suite.
FAST_TIMEOUTS = {
    "command_timeout": 0.5,
    "startup_timeout": 0.5,
    "scan_timeout": 1.0,
    "pana_timeout": 0.5,
    "echonet_timeout": 0.5,
}


def fast_session_config(auth_id: str, password: str) -> SessionConfig:
    """Build a session config with the short test timeouts."""
    return SessionConfig(auth_id=auth_id, password=password, **FAST_TIMEOUTS)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Let Home Assistant load the custom integration in every test."""
    return


@pytest.fixture
def behaviour() -> AdapterBehaviour:
    """Adapter behaviour a test can mutate before the integration talks to it."""
    return AdapterBehaviour(auth_id=AUTH_ID, password=PASSWORD)


@pytest.fixture
def adapter(behaviour: AdapterBehaviour) -> Generator[FakeAdapter]:
    """Replace the serial transport with the in-memory adapter."""
    fake = FakeAdapter(behaviour)
    with (
        patch("custom_components.broute_j11.SerialTransport", return_value=fake),
        patch("custom_components.broute_j11.SessionConfig", fast_session_config),
        patch(
            "custom_components.broute_j11.config_flow.SerialTransport",
            return_value=fake,
        ),
        patch(
            "custom_components.broute_j11.config_flow.SessionConfig",
            fast_session_config,
        ),
        patch(
            "custom_components.broute_j11.config_flow.list_ports.comports",
            return_value=[],
        ),
    ):
        yield fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry for the synthetic meter."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        unique_id=meter_identifier(AUTH_ID),
        title="Smart meter",
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, adapter: FakeAdapter, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration against the in-memory adapter."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
