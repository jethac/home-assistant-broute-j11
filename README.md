# Home Assistant B-route J11

A planned Home Assistant custom integration for Japanese B-route smart meters connected through the RATOC Systems RS-WSUHA-J11 or compatible ROHM BP35C0-J11/BP35C2-J11-T01 hardware.

## Status

This repository currently contains the product requirements and implementation brief. The integration is not implemented yet.

The important distinction is that J11 adapters use ROHM's binary UART protocol. They do not accept the text-based `SK...` commands used by BP35A1, BP35C2, RS-WSUHA-P, and many existing B-route tools. A driver that waits for an `SKVER` response will appear to hang even when an RS-WSUHA-J11 is working normally.

## Goal

Build a Python-native Home Assistant integration that:

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

## Intended Home Assistant entities

| Entity | Unit | Home Assistant semantics |
| --- | --- | --- |
| Instantaneous power | W | `device_class: power`, `state_class: measurement` |
| Instantaneous current, R phase | A | `device_class: current`, `state_class: measurement` |
| Instantaneous current, T phase | A | `device_class: current`, `state_class: measurement` |
| Cumulative forward energy | kWh | `device_class: energy`, `state_class: total_increasing` |
| Cumulative reverse energy | kWh | `device_class: energy`, `state_class: total_increasing` |

Forward energy is intended for grid consumption. Reverse energy is intended for return to grid.

## Proposed repository layout

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
