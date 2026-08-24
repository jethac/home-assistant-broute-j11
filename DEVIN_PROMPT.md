# Devin implementation prompt

Use the following prompt as the complete implementation assignment.

---

You are implementing the first working release of `jethac/home-assistant-broute-j11`.

Start by reading `README.md` and `PRD.md` in full. Treat `PRD.md` as the product contract. Build a Python-native Home Assistant custom integration for RATOC RS-WSUHA-J11 / ROHM BP35C0-J11 binary-UART adapters. Do not substitute an MQTT bridge, Go daemon, shell command, Supervisor add-on, or cloud service.

## Working rules

1. Create a feature branch and deliver the work as one or more reviewable pull requests. Do not push implementation commits directly to `main`.
2. Use test-driven development for every protocol behavior and bug fix: write one focused failing test, run it and confirm the expected failure, implement the minimum code, then run it again.
3. Never request, store, print, or commit real B-route credentials, meter identifiers, MAC addresses, PAN IDs, USB serial numbers, IPv6 addresses, or raw private frames. Use synthetic fixtures. Hardware credentials may be supplied later through environment variables for an opt-in local test only.
4. Read the current official Home Assistant developer documentation before choosing manifest fields, config-flow APIs, coordinator patterns, entity metadata, diagnostics APIs, and test helpers.
5. Read the ROHM J11 UART specification and B-route application note. Do not infer the binary framing or command payloads from similarly named text-command adapters.
6. Use `ak1211/BRouteJ11` only under its MIT terms. If you translate or derive code from it, preserve its copyright and license notice in affected files and document the derivation.
7. Keep protocol parsing independent of Home Assistant and serial I/O. The codec and ECHONET modules must be testable as pure code.
8. Never block Home Assistant's event loop with serial I/O.

## Deliverables

Create the structure described in `README.md`, including:

- `custom_components/broute_j11/manifest.json`
- config flow and polling-interval options flow
- config-entry setup, unload, and reload
- a coordinator/session boundary that performs blocking UART work outside the event loop
- J11 frame codec with incremental buffering, checksum validation, bounded lengths, and response/notification routing
- typed encoders/decoders for reset, initialization, B-route credential configuration, active scan, PANA connection, UDP/ECHONET transmission, and required notifications
- ECHONET Lite parsing for coefficient, cumulative unit, forward energy, reverse energy, instantaneous power, and R/T phase current
- Energy-dashboard-compatible sensors with exact metadata from `PRD.md`
- automatic reconnect with bounded timeouts and capped backoff
- diagnostics with comprehensive redaction
- English and Japanese translations
- automated tests and CI
- installation, USB passthrough, Energy dashboard, troubleshooting, and contributor documentation

## Required implementation order

Work in vertical, reviewable slices:

1. Establish packaging, test tooling, and CI skeleton.
2. Implement the frame codec using golden vectors from the ROHM specification.
3. Implement only the J11 commands required for reset and initialization.
4. Add scan, credential configuration, PANA connection, and reconnect state handling.
5. Add ECHONET GET encoding and property parsing with unit conversion.
6. Add Home Assistant config flow and lifecycle.
7. Add sensors, diagnostics, translations, and user documentation.
8. Add an opt-in hardware validation tool that reads secrets only from environment variables and never logs them.

Do not create speculative abstractions or implement Enhanced HAN device management. One adapter and one meter per config entry is sufficient.

## Testing expectations

At minimum, tests must cover every item in section 9 of `PRD.md`. For each new production function, ensure a test existed and failed for the intended reason before implementation. Avoid mocks when an in-memory serial transport or pure input/output test can exercise real behavior.

Run the full lint, type-check, unit-test, Home Assistant-test, coverage, and secret-scan suite before opening the final PR. Include the exact commands and results in the PR description. Protocol branch coverage must be at least 90%.

## Definition of done

The work is done only when all acceptance criteria in section 13 of `PRD.md` are satisfied or clearly split between CI-verifiable criteria and a documented, user-run hardware validation checklist. Do not claim hardware success without evidence from an RS-WSUHA-J11.

Before coding, post a concise implementation plan in the issue or PR describing file boundaries, test strategy, upstream-license handling, and how serial cancellation will work. If the official specifications conflict with this prompt or the PRD, stop and document the conflict with exact citations before proceeding.

---
