# Test fixtures

Every fixture in this directory is synthetic. Two families exist, and neither
describes a real installation:

1. **Documented golden vectors** — byte-for-byte frame samples published in
   ROHM's public B-route application note (`BP35C0-J11 B-Route Communication`,
   No. 63AN028E, §5.1–§5.4) and UART IF specification (No. 63TR008E). They use
   the documentation's own placeholder authentication ID
   (`00112233445566778899AABBCCDDEEFF`), password (`0123456789AB`) and MAC
   address, so the encoders can be checked against the specification rather than
   against themselves.
2. **Repository-invented values** — an authentication ID ending in `ABCD`, the
   password `SYNTHETICPW1` and the MAC address `A8BBCCDDEEFF0011`, used where a
   test needs a value the specification does not provide.

Both families are allowlisted by `scripts/secret_scan.py`. Any other
credential-shaped or identifier-shaped value in the repository fails that scan.

`frames.py` documents, per constant, which command or notification the frame
represents.
