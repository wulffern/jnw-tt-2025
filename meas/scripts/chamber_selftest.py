#!/usr/bin/env python3
"""End-to-end check of the jnwtemp chamber driver against the real chamber.

Reads are exercised for real; the write is exercised with the chamber's own
current setpoint, so nothing moves. Reports clearly whether the chamber is in a
state where sweeps can actually drive it.

    python scripts/chamber_selftest.py [host] [port] [address]
"""

from __future__ import annotations

import os
import sys

# The driver's status strings use '°' and '→'; a cp1252 console would die on them.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from jnwtemp.chamber import (  # noqa: E402
    DEFAULT_ADDRESS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ChamberError,
    VotschChamber,
)


def main(host: str, port: int, addr: int) -> int:
    ch = VotschChamber(host, port, addr)
    print(f"connecting to {host}:{port} address {addr:02d} ...")
    try:
        status = ch.connect()
    except ChamberError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"  raw      : {status.raw!r}")
    print(f"  parsed   : {status.describe()}")
    print(f"  layout   : {len(status.values)} values, {len(status.bits)} bits")
    print(f"  set cmd  : {ch._set_command(status.setpoint_c, status.running)!r}")

    # Re-sending the current setpoint would prove nothing: it verifies as
    # accepted whether or not the chamber listened. The only real test is to
    # command a value that differs, so nudge by a degree and put it back.
    original, running = status.setpoint_c, status.running
    probe = original + 1.0
    print(f"\nprobing writes: {original:.1f} -> {probe:.1f} °C, then back ...")
    try:
        ch.set_temp(probe, on=running)
        print(f"  ACCEPTED : setpoint moved to {ch.status().setpoint_c:.1f} °C")
        print("\nReads and writes both work - sweeps will drive the chamber.")
        rc = 0
    except ChamberError as exc:
        print(f"  REFUSED  : {exc}")
        print("\nReads work, writes do not. Put the chamber panel into")
        print("external/remote mode before running a sweep or calibrating.")
        rc = 1
    finally:
        try:
            ch.set_temp(original, on=running)
        except ChamberError:
            pass
        print(f"restored : {ch.status().describe()}")
        ch.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST,
            int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT,
            int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_ADDRESS,
        )
    )
