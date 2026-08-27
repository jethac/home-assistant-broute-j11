# Contributing

## Development environment

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ../broute-j11 -e ".[dev]"
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

CI runs the same commands. The Home Assistant integration must keep branch
coverage at 90% or above.

## Design boundaries

| Repository/module | Responsibility |
| --- | --- |
| `broute-j11` | Binary framing, commands, ECHONET Lite, serial transport, session lifecycle, and reconnect behavior. |
| `custom_components/broute_j11/` | Home Assistant config entries, flows, coordinator, entities, and diagnostics. |
| `tests/fixtures/fake_adapter.py` | Synthetic in-memory adapter used only at the integration boundary. |

Protocol changes and their tests belong in the `broute-j11` repository. Keep
the integration pinned to an exact reviewed library release.

## Testing

Write a focused failing test before fixing an integration bug or adding a
behavior. Integration tests run against the in-memory adapter in
`tests/fixtures/fake_adapter.py`, which speaks the real binary protocol; prefer
extending its `AdapterBehaviour` over weakening Home Assistant behavior
assertions. Run the standalone library suite for protocol changes.

Never commit real credentials, meter identifiers, MAC addresses, PAN IDs, USB
serial numbers or captured frames. Fixtures use synthetic values, and
`scripts/secret_scan.py` fails the build when something that looks private
appears in the tree.

## Hardware validation

CI cannot verify radio behavior. Changes to the standalone library's session
state machine should be confirmed with its hardware validator against a real
adapter. Integration releases should also run `tools/validate_hardware.py` as
described in the README, and quote the result in the pull request.

## Upstream license

`ak1211/BRouteJ11` is MIT licensed. No code in this repository is translated
from it; it was used as a protocol cross-reference only. If a future change does
derive code from it, keep its copyright and license notice in the affected files
and note the derivation there.
