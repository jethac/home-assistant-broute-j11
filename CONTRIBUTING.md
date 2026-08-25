# Contributing

## Development environment

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pre-commit install
```

`pre-commit` runs Ruff (format and lint) and the repository secret scan on
every commit.

## Checks

```bash
.venv/bin/ruff format .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest -q --cov=custom_components/broute_j11 --cov-report=term-missing
.venv/bin/python scripts/secret_scan.py
```

CI runs the same commands. Protocol code must keep branch coverage at 90% or
above.

## Design boundaries

| Module | Responsibility |
| --- | --- |
| `protocol/codec.py` | Frame headers, checksums, incremental reassembly. Pure bytes in, frames out. |
| `protocol/commands.py` | J11 request builders and response/notification parsers. No I/O. |
| `protocol/echonet.py` | ECHONET Lite frames, property decoding, unit conversion. No I/O. |
| `protocol/transport.py` | Blocking pyserial calls, nothing else. |
| `protocol/session.py` | Asynchronous lifecycle: reset, scan, PANA, polling, reconnect. |
| Everything else | Home Assistant glue only. |

The codec, command and ECHONET modules must stay importable without Home
Assistant and without a serial port, and serial I/O must always run through
`run_in_executor` so the event loop is never blocked.

## Testing

Write a focused failing test before fixing a protocol bug or adding a
behaviour. Protocol and integration tests run against the in-memory adapter in
`tests/fixtures/fake_adapter.py`, which speaks the real binary protocol; prefer
extending its `AdapterBehaviour` over patching production code.

Never commit real credentials, meter identifiers, MAC addresses, PAN IDs, USB
serial numbers or captured frames. Fixtures use synthetic values, and
`scripts/secret_scan.py` fails the build when something that looks private
appears in the tree.

## Hardware validation

CI cannot verify radio behaviour. Changes to the session state machine should be
confirmed with `tools/validate_hardware.py` against a real adapter, as described
in the README, and the result quoted in the pull request.

## Upstream license

`ak1211/BRouteJ11` is MIT licensed. No code in this repository is translated
from it; it was used as a protocol cross-reference only. If a future change does
derive code from it, keep its copyright and license notice in the affected files
and note the derivation there.
