#!/usr/bin/env python3
r"""Opt-in end-to-end check against a real RS-WSUHA-J11 adapter.

Run this outside Home Assistant to confirm that an adapter, a set of Route-B
credentials and a meter work together. Credentials are read from the
environment only, are never echoed, and nothing this script prints identifies
the meter:

    BROUTE_J11_DEVICE=/dev/serial/by-id/usb-... \\
    BROUTE_J11_AUTH_ID=... \\
    BROUTE_J11_PASSWORD=... \\
    python tools/validate_hardware.py

Exits non-zero when the session cannot be established or the meter cannot be
read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.broute_j11.protocol.codec import ProtocolError
from custom_components.broute_j11.protocol.session import (
    J11Session,
    SessionConfig,
)
from custom_components.broute_j11.protocol.transport import (
    SerialTransport,
    TransportError,
)

ENV_DEVICE = "BROUTE_J11_DEVICE"
ENV_AUTH_ID = "BROUTE_J11_AUTH_ID"
ENV_PASSWORD = "BROUTE_J11_PASSWORD"


def read_environment() -> tuple[str, SessionConfig]:
    """Return the device path and session config from the environment."""
    missing = [
        name
        for name in (ENV_DEVICE, ENV_AUTH_ID, ENV_PASSWORD)
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} before running this tool")
    return os.environ[ENV_DEVICE], SessionConfig(
        auth_id=os.environ[ENV_AUTH_ID],
        password=os.environ[ENV_PASSWORD],
    )


async def validate(polls: int, interval: float) -> int:
    """Pair with the meter, read it ``polls`` times and report the results."""
    device, config = read_environment()
    session = J11Session(SerialTransport(device), config)
    try:
        link = await session.async_connect()
    except (ProtocolError, TransportError) as err:
        print(f"FAIL: could not establish the B-route session: {err}")
        return 1
    print(f"PASS: PANA established on channel {link.channel}, RSSI {link.rssi} dBm")
    print(f"      adapter firmware {link.firmware_version}")
    profile = session.profile
    print(
        f"      meter coefficient {profile.coefficient}, "
        f"unit {profile.unit}, {profile.digits} digits"
    )
    status = 0
    try:
        for poll in range(1, polls + 1):
            if poll > 1:
                await asyncio.sleep(interval)
            try:
                reading = await session.async_read_meter()
            except (ProtocolError, TransportError) as err:
                print(f"FAIL: poll {poll} failed: {err}")
                status = 1
                continue
            print(
                f"PASS: poll {poll}: {reading.instantaneous_power} W, "
                f"R {reading.current_r_phase} A, T {reading.current_t_phase} A, "
                f"forward {reading.cumulative_forward_energy} kWh, "
                f"reverse {reading.cumulative_reverse_energy} kWh"
            )
    finally:
        await session.async_close()
    stats = session.stats
    print(
        f"      {stats.requests} requests, {stats.timeouts} timeouts, "
        f"{stats.echonet_retries} ECHONET retries, "
        f"{stats.frames.discarded_bytes} discarded bytes"
    )
    return status


def main() -> int:
    """Parse the arguments and run the validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--polls", type=int, default=3, help="how many readings to take"
    )
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between readings"
    )
    parser.add_argument("--debug", action="store_true", help="log protocol details")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(validate(args.polls, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
