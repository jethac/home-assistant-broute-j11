# Product Requirements Document

## 1. Product summary

`home-assistant-broute-j11` is a Python-native Home Assistant custom integration for a Japanese low-voltage smart electricity meter reached through a RATOC Systems RS-WSUHA-J11 or compatible ROHM J11 Wi-SUN USB adapter.

The integration owns the complete local data path:

```text
Home Assistant
  -> Python integration
  -> J11 binary UART
  -> Wi-SUN B-route/PANA
  -> ECHONET Lite smart meter
```

It must not require MQTT, a Supervisor add-on, a separate process, or a cloud service.

## 2. Problem statement

Most open-source B-route integrations target adapters that expose the text-based SKSTACK-IP command interface. The RS-WSUHA-J11 contains a BP35C0-J11 and instead uses a framed binary UART command protocol. Text commands such as `SKVER`, `SKSCAN`, and `SKJOIN` receive no response.

Home Assistant users need a native integration that understands J11 framing, performs B-route pairing, parses ECHONET Lite meter properties, and provides Energy-dashboard-compatible entities.

## 3. Goals

1. Configure one J11 adapter and one B-route smart meter through the Home Assistant UI.
2. Perform and persist enough pairing state to reconnect without a full active scan when possible.
3. Report grid import, grid export, instantaneous power, and phase current using correct Home Assistant metadata.
4. Recover automatically from USB interruptions, malformed frames, timeouts, radio loss, and PANA-session loss.
5. Keep credentials and stable personal/device identifiers out of logs, diagnostics, tests, and source control.
6. Maintain strong automated coverage for protocol code without requiring physical hardware in CI.

## 4. Non-goals for the first release

- RS-WSUHA-P, BP35A1, or text-based SKSTACK-IP adapters
- Multiple adapters or multiple meters per config entry
- Enhanced HAN device management beyond what is required for B-route
- Tariff calculation, electricity pricing, or utility-bill forecasting
- Automatic Energy dashboard mutation
- Home Assistant OS add-on packaging
- HACS default-repository publication
- Firmware updates for the adapter

## 5. Users and primary workflow

The primary user runs Home Assistant Core or Home Assistant Container on Linux and has passed a supported USB adapter into the Home Assistant process/container.

Configuration flow:

1. The user selects `B-route Smart Meter (J11)` from Add Integration.
2. The integration lists serial devices and highlights known compatible USB devices when available.
3. The user selects a device and enters the 32-character B-route authentication ID and 12-character password.
4. The integration validates formatting locally without logging either value.
5. The integration resets and initializes the J11 module, configures B-route credentials, performs an active scan, and establishes a PANA session.
6. On success, the config entry is created and sensor entities become available.
7. The user manually assigns forward and reverse cumulative energy sensors to grid consumption and return-to-grid in the Energy dashboard.

If setup cannot complete, the flow reports a specific retryable error: serial device unavailable, adapter unsupported, no meter found, authentication rejected, or connection timeout.

## 6. Functional requirements

### 6.1 Serial transport

- Open the selected device at the UART settings required by the J11 hardware specification.
- Use asynchronous Home Assistant orchestration while all blocking serial I/O runs outside the event loop.
- Frame incoming bytes incrementally; reads may contain partial frames or multiple frames.
- Validate frame headers, declared lengths, command codes, and checksums before dispatch.
- Bound receive buffers and reject impossible lengths.
- Correlate command responses without losing asynchronous notification frames.
- Close the port cleanly during unload and before retrying.

### 6.2 J11 module lifecycle

- Issue the documented hardware-reset request and wait for the boot-complete notification.
- Perform the required initial setup for B-route operation.
- Configure B-route authentication information using the binary command defined by the J11 specification.
- Perform an active scan on first setup or when cached network state fails.
- Persist only the minimum reconnect state: channel, PAN ID, and meter MAC/address data.
- Establish and monitor the PANA session.
- Reconnect using cached state first, then fall back to a new scan.
- Apply bounded timeouts and retry delays; never wait indefinitely for a serial response.

### 6.3 ECHONET Lite

- Send ECHONET Lite GET requests to the low-voltage smart electric energy meter object (`0x028801`).
- Discover or read the coefficient and cumulative-energy unit before converting totals to kWh.
- Support these properties when advertised by the meter:
  - cumulative forward energy;
  - cumulative reverse energy;
  - instantaneous power;
  - instantaneous current for R and T phases;
  - manufacturer code, protocol/version information, and manufacturing number for diagnostics.
- Treat unsupported optional properties as unavailable rather than failing the entire integration.
- Reject malformed, mismatched-transaction, or wrong-object ECHONET responses.
- Preserve numeric precision needed for monotonic cumulative energy statistics.

### 6.4 Home Assistant entities

Required enabled-by-default sensors:

| Key | Native unit | Device class | State class |
| --- | --- | --- | --- |
| `instantaneous_power` | W | `power` | `measurement` |
| `instantaneous_current_r` | A | `current` | `measurement` |
| `instantaneous_current_t` | A | `current` | `measurement` |
| `cumulative_forward_energy` | kWh | `energy` | `total_increasing` |
| `cumulative_reverse_energy` | kWh | `energy` | `total_increasing` |

Entity requirements:

- Use stable unique IDs derived from redacted-safe adapter/meter identifiers, not credentials.
- Associate all entities with one Home Assistant device.
- Expose availability from coordinator/session health.
- Do not emit zero or stale values when the current value is unknown.
- Handle genuine meter counter rollover/reset according to Home Assistant long-term-statistics semantics.
- Use translations for user-facing names; initially provide English and Japanese.

### 6.5 Updates and options

- Default instantaneous polling interval: 60 seconds.
- Allow a bounded options range of 30 to 300 seconds.
- Do not poll cumulative values more aggressively than instantaneous values.
- Accept asynchronous meter notifications when available, but correctness must not depend on them.
- Serialize requests so only one command transaction owns the UART at a time.

### 6.6 Configuration and lifecycle

- Implement UI config flow only; YAML configuration is not required.
- Prevent duplicate entries for the same adapter/meter combination.
- Support config-entry unload and reload without restarting Home Assistant.
- Register an options flow for the polling interval.
- Surface actionable repair/setup errors when the configured serial path disappears.
- Prefer stable `/dev/serial/by-id/...` paths when Home Assistant provides them.

### 6.7 Diagnostics and logging

- Diagnostics may include integration version, adapter model classification, protocol state, retry counters, and last-success timestamps.
- Diagnostics must redact authentication ID, password, meter MAC address, manufacturing number, USB serial number, PAN ID, IPv6 address, and raw frames containing any of those values.
- INFO logs describe lifecycle transitions without identifiers.
- DEBUG logs may describe command names, sizes, and result codes, but never raw credential-bearing payloads.
- Exceptions must not include outbound frame bytes for credential commands.

## 7. Architecture

### 7.1 Protocol codec

`protocol/codec.py` provides pure functions or a small stateful decoder for binary framing, length validation, and checksum calculation. It has no Home Assistant imports and no serial dependency.

### 7.2 Command model

`protocol/commands.py` defines typed request encoders and response/notification decoders for only the command set required by this product. Unknown command codes are represented safely and logged at debug level.

### 7.3 ECHONET model

`protocol/echonet.py` encodes GET requests, parses responses, validates transaction/object fields, and converts raw meter properties to typed values. Unit conversion is isolated and exhaustively tested.

### 7.4 Session

`protocol/session.py` owns serial transport, module initialization, scanning, PANA establishment, request correlation, retries, and reconnect state. Its serial dependency is injected so tests can use deterministic in-memory transports.

### 7.5 Home Assistant adapter

The config flow validates user input and performs initial pairing. A `DataUpdateCoordinator` invokes the session outside the event loop and distributes typed readings to sensor entities. Platform code contains no binary parsing.

## 8. Error-handling requirements

- Every serial command has a finite timeout.
- Retry only errors that can plausibly recover: timeouts, disconnects, transient scan failure, and session loss.
- Authentication failure stops automatic rapid retries and becomes a user-actionable config-entry error.
- Use capped exponential backoff with jitter for background reconnects.
- A malformed frame increments a diagnostic counter and is discarded; repeated framing failure forces a clean transport reset.
- Cancellation and Home Assistant shutdown interrupt waits promptly.
- Never rewrite adapter nonvolatile settings unless explicitly required and documented.

## 9. Testing requirements

### 9.1 Unit tests

- Checksum golden vectors and corruption rejection
- Fragmented frame assembly
- Multiple frames in one read
- Invalid header and impossible length handling
- Every implemented request encoder and response decoder
- Interleaved response and notification routing
- ECHONET transaction/object validation
- Coefficient/unit conversion for forward and reverse energy
- Signed/unsigned instantaneous values and phase-current sentinel values
- Retry, timeout, cancellation, and reconnect state machines
- Redaction of all sensitive fields

### 9.2 Home Assistant tests

- Successful config flow and duplicate rejection
- Invalid credential format without contacting hardware
- Serial unavailable, no-meter, timeout, and authentication errors
- Config-entry setup, unload, reload, and options flow
- Entity metadata, availability, state restoration behavior, and statistics compatibility
- Diagnostics redaction

### 9.3 Test fixtures

- Fixtures must be synthetic or irreversibly sanitized.
- No real B-route credentials or stable device identifiers.
- Each fixture documents the command/notification it represents.
- Hardware-in-the-loop tests are opt-in, excluded from CI, and obtain secrets only from environment variables.

## 10. Quality requirements

- Follow current Home Assistant custom-integration conventions.
- Support the Python version used by the current Home Assistant stable release.
- Type-check production code and keep protocol APIs typed.
- Format and lint with the repository's selected tools.
- Maintain at least 90% branch coverage for `protocol/`.
- CI must run linting, type checking, unit tests, and Home Assistant integration tests.
- Pin GitHub Actions by immutable commit SHA before the first release.

## 11. Documentation requirements

Before the first release, README documentation must include:

- exact supported and unsupported adapter families;
- Home Assistant Container USB passthrough example;
- installation and config-flow steps;
- Energy dashboard mapping instructions;
- troubleshooting for permission errors, missing serial devices, scan failure, and authentication failure;
- credential-handling warning;
- contributor setup and test commands.

## 12. Milestones

1. Protocol codec and command golden tests
2. J11 initialization, scan, credential configuration, and PANA session
3. ECHONET Lite property parsing and typed readings
4. Home Assistant config flow, coordinator, and sensors
5. Reconnect behavior, diagnostics, translations, and documentation
6. Hardware validation with RS-WSUHA-J11 and a real smart meter
7. Tagged `v0.1.0` pre-release

## 13. Acceptance criteria

The first release candidate is acceptable when:

1. A fresh Home Assistant installation can configure a passed-through RS-WSUHA-J11 entirely through the UI.
2. Valid credentials establish a B-route PANA session and produce readings without MQTT or another process.
3. Forward and reverse cumulative energy sensors are accepted by the Energy dashboard.
4. Instantaneous power and both phase-current sensors update for at least 24 hours under hardware testing.
5. Unplugging and reconnecting the adapter recovers without restarting Home Assistant.
6. A lost PANA session reconnects automatically with bounded backoff.
7. All automated tests pass, protocol branch coverage is at least 90%, and diagnostics tests prove sensitive values are redacted.
8. A repository-wide secret scan finds no live credentials or stable private device identifiers.

## 14. References and licensing

Normative protocol behavior comes from ROHM's BP35C0-J11 UART interface specification and B-route application note. RATOC documentation defines the supported packaged adapter.

The MIT-licensed [ak1211/BRouteJ11](https://github.com/ak1211/BRouteJ11) project is a useful behavioral reference. If code is translated or derived rather than independently implemented from specifications, retain its copyright and MIT license notice in the derived files and repository notices.
