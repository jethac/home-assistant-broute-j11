"""Tests for diagnostics redaction."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.broute_j11.diagnostics import (
    async_get_config_entry_diagnostics,
)

from . import conftest
from .fixtures import fake_adapter as fake


async def test_nothing_identifying_is_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)
    dumped = json.dumps(diagnostics)
    for secret in (
        conftest.AUTH_ID,
        conftest.PASSWORD,
        conftest.DEVICE,
        fake.METER_MAC.hex(),
        fake.METER_MAC.hex().upper(),
        f"{fake.METER_PAN_ID:04X}",
        "SYNTHETIC001",
    ):
        assert secret not in dumped
    assert diagnostics["session"]["connected"] is True
    assert diagnostics["session"]["channel"] == fake.METER_CHANNEL
    assert diagnostics["statistics"]["connects"] == 1
