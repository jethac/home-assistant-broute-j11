# Test fixtures

`fake_adapter.py` is a synthetic, in-memory J11 adapter used to exercise the
Home Assistant boundary without a serial device. It speaks the real library
transport contract but contains no data from a real installation.

Its credentials and identifiers are either repository-invented values or the
placeholder meter identity published in ROHM's documentation. They are
allowlisted by `scripts/secret_scan.py`; any other credential-shaped or
identifier-shaped value in the repository fails that scan.
