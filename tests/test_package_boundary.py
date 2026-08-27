"""Dependency-boundary tests for the Home Assistant integration."""

from __future__ import annotations

import json
from pathlib import Path

import broute_j11

from custom_components import broute_j11 as integration
from custom_components.broute_j11 import config_flow, coordinator, sensor


def test_manifest_pins_the_reviewed_library_release() -> None:
    """Home Assistant installs the exact library release reviewed here."""
    component_dir = Path(integration.__file__).parent
    manifest = json.loads((component_dir / "manifest.json").read_text())

    assert manifest["requirements"] == ["broute-j11==0.1.0"]


def test_runtime_imports_come_from_the_installed_library() -> None:
    """Integration runtime types are the installed package's public API."""
    assert integration.J11Session is broute_j11.J11Session
    assert config_flow.J11Session is broute_j11.J11Session
    assert coordinator.J11Session is broute_j11.J11Session
    assert sensor.MeterReading is broute_j11.MeterReading


def test_integration_does_not_bundle_a_protocol_package() -> None:
    """Protocol implementation has one owner: the installed library."""
    component_dir = Path(integration.__file__).parent

    assert not (component_dir / "protocol").exists()
