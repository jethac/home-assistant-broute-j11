# Home Assistant B-route J11

A Home Assistant custom integration for Japanese B-route smart meters connected through the RATOC Systems RS-WSUHA-J11 or compatible ROHM BP35C0-J11/BP35C2-J11-T01 hardware.

## Status

The integration is implemented and covered by automated tests against an in-memory adapter that speaks the real binary protocol. It has not yet been validated against physical hardware; see [Hardware validation](#hardware-validation).

The important distinction is that J11 adapters use ROHM's binary UART protocol. They do not accept the text-based `SK...` commands used by BP35A1, BP35C2, RS-WSUHA-P, and many existing B-route tools. A driver that waits for an `SKVER` response will appear to hang even when an RS-WSUHA-J11 is working normally.

## Goal

A Python-native Home Assistant integration that:

- communicates directly with the J11 binary UART protocol;
- pairs with one low-voltage smart electricity meter using B-route credentials;
- exposes instantaneous power and current;
- exposes cumulative forward and reverse energy suitable for Home Assistant's Energy dashboard;
- reconnects after transient serial, radio, and PANA-session failures; and
- never exposes B-route credentials in logs, diagnostics, fixtures, or repository history.

No MQTT broker, sidecar service, or external daemon should be required.

## Target hardware

Initial support is deliberately narrow:

- RATOC Systems RS-WSUHA-J11
- ROHM BP35C2-J11-T01
- Other adapters using BP35C0-J11 may work after their USB and UART behavior is verified

The similarly named RS-WSUHA-P/BP35C2 uses a different command protocol and is out of scope for the first release.

## Home Assistant entities

| Entity | Unit | Home Assistant semantics |
| --- | --- | --- |
| Instantaneous power | W | `device_class: power`, `state_class: measurement` |
| Instantaneous current, R phase | A | `device_class: current`, `state_class: measurement` |
| Instantaneous current, T phase | A | `device_class: current`, `state_class: measurement` |
| Cumulative forward energy | kWh | `device_class: energy`, `state_class: total_increasing` |
| Cumulative reverse energy | kWh | `device_class: energy`, `state_class: total_increasing` |

Forward energy is intended for grid consumption. Reverse energy is intended for return to grid.

## Installation

1. Copy `custom_components/broute_j11` into your Home Assistant configuration directory, so that `<config>/custom_components/broute_j11/manifest.json` exists.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration** and choose **B-route Smart Meter (J11)**.
4. Select the adapter's serial device, then enter the Route-B authentication ID (32 hexadecimal characters) and password (12 letters and digits) your electricity retailer sent you.

Pairing takes up to a minute: the adapter is reset, scans every channel for your meter, and completes a PANA authentication before the entry is created.

Prefer a stable path such as `/dev/serial/by-id/usb-RATOC_Systems__Ltd._RS-WSUHA-J11-if00-port0` over `/dev/ttyUSB0`. The dropdown lists what is currently attached, and any path can be typed in directly.

### Polling

The meter is read every 60 seconds by default. Change it under the integration's **Configure** menu; 30 to 300 seconds is accepted. Meters answer slowly, so shorter intervals mostly increase the chance of a timeout.

## USB passthrough

The adapter must be visible inside the container or virtual machine that runs Home Assistant.

- **Home Assistant OS / Supervised**: nothing to do beyond plugging the adapter in; the device appears in the config flow.
- **Docker**: pass the device through, using the by-id path so it survives re-plugging.

  ```yaml
  services:
    homeassistant:
      devices:
        - /dev/serial/by-id/usb-RATOC_Systems__Ltd._RS-WSUHA-J11-if00-port0:/dev/serial/by-id/usb-RATOC_Systems__Ltd._RS-WSUHA-J11-if00-port0
  ```

- **Proxmox / other VMs**: attach the USB device to the guest, not the host, and prefer binding by vendor/product ID so a reboot does not move it.
- **Permissions**: the Home Assistant user needs read/write access to the device, which usually means membership of `dialout` on bare-metal installs.

The adapter presents a CDC-ACM/serial interface at 115,200 bps, 8N1, with flow control disabled. No driver installation is required on Linux.

## Energy dashboard

The cumulative sensors are `total_increasing` energy sensors in kWh, which is what the Energy dashboard expects.

1. Go to **Settings → Dashboards → Energy**.
2. Under **Grid consumption**, add **Smart meter Cumulative energy consumed**.
3. Under **Return to grid**, add **Smart meter Cumulative energy returned** if you have solar.

Use the cumulative sensors, not instantaneous power: the meter's own counters are the authoritative totals, and long-term statistics are generated from them. Meters without a reverse-energy counter leave that sensor unavailable, which is expected.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| "The serial device could not be opened" | The adapter is not attached to Home Assistant, or the path changed. Check USB passthrough and use a `/dev/serial/by-id/...` path. |
| "The meter rejected these credentials" | The authentication ID or password is wrong. They are meter-specific and are re-issued by the retailer; the integration deliberately stops retrying so the meter does not lock you out. |
| "No smart meter answered the scan" | The credentials belong to a different meter, or the adapter is too far away or behind too much metal. Try the adapter on a USB extension closer to the meter. |
| "The adapter stopped responding" | The adapter's firmware is wedged. Unplug it, wait a few seconds, plug it back in and reload the entry. |
| Sensors become unavailable, then recover | Normal: the radio link or PANA session dropped and the integration reconnected with backoff. |
| Sensors stay unavailable | Check the log for the last session error, and collect diagnostics from the integration's menu. Diagnostics are redacted and safe to attach to an issue. |
| Nothing works and the log mentions `SKVER` or text commands | Another integration is competing for the adapter. J11 hardware does not speak the text protocol. |

Enable protocol logging when reporting a problem:

```yaml
logger:
  logs:
    custom_components.broute_j11: debug
```

Debug logs contain command codes and results but not credentials.

## Hardware validation

CI cannot prove radio behaviour, so there is an opt-in tool that pairs with a real meter outside Home Assistant. It reads credentials only from the environment and never prints them:

```bash
BROUTE_J11_DEVICE=/dev/serial/by-id/usb-... \
BROUTE_J11_AUTH_ID=... \
BROUTE_J11_PASSWORD=... \
.venv/bin/python tools/validate_hardware.py --polls 3 --interval 60
```

It reports the channel, RSSI, adapter firmware, meter scaling, one line per reading and the protocol counters, and exits non-zero on failure.

## Repository layout

```text
custom_components/broute_j11/
  __init__.py
  config_flow.py
  const.py
  coordinator.py
  diagnostics.py
  manifest.json
  sensor.py
  strings.json
  translations/
  protocol/
    codec.py
    commands.py
    echonet.py
    session.py
tests/
  fixtures/
  test_codec.py
  test_commands.py
  test_config_flow.py
  test_echonet.py
  test_session.py
  test_sensor.py
```

The protocol package belongs inside the integration so installation remains a single copy operation, while retaining clear boundaries and unit-testable APIs.

## Security

B-route authentication IDs and passwords are secrets.

- Never put real credentials, meter identifiers, MAC addresses, USB serial numbers, or unredacted diagnostics in this repository.
- Store credentials only in the Home Assistant config entry.
- Mark password fields as secret in the config flow.
- Redact credentials and stable device identifiers from diagnostics and exceptions.
- Use synthetic protocol frames in automated tests.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development environment, the checks CI runs, and the module boundaries.

## Product requirements

See [PRD.md](PRD.md) for the complete scope and acceptance criteria.

## Implementation handoff

See [DEVIN_PROMPT.md](DEVIN_PROMPT.md) for a self-contained implementation assignment suitable for an autonomous development agent.

## Primary references

- [RATOC RS-WSUHA series product page](https://www.ratocsystems.com/products/wisun/usb-wisun/rs-wsuha/)
- [RATOC RS-WSUHA-J11 startup guide](https://www.ratocsystems.com/dlmanual/wisun/wsuhaj11startguide/)
- [ROHM BP35C0-J11 UART interface specification](https://fscdn.rohm.com/en/products/databook/applinote/module/wireless/bp35c0-j11_uartif_specification_tr-e.pdf)
- [ROHM BP35C0-J11 B-route communication application note](https://fscdn.rohm.com/en/products/databook/applinote/module/wireless/bp35c0-j11_b-route_an-e.pdf)
- [ak1211/BRouteJ11](https://github.com/ak1211/BRouteJ11), an MIT-licensed Go reference implementation
- [Home Assistant developer documentation](https://developers.home-assistant.io/)

## License

MIT. Any implementation derived from another MIT-licensed project must retain the required copyright and license notices.
