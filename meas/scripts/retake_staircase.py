#!/usr/bin/env python3
"""Retake the 1 s per-event capture that slide 06's staircase histogram needs.

The staircase is the one figure in the deck that cannot come from the demo
board. It shows every individual GR07 period landing on a handful of discrete
values one 64 MHz clock cycle apart, and resolving that needs a timebase finer
than the 15.6 ns step being measured. The board's PIO tick is 23.4 ns - coarser
than the effect - so only the Saleae can do it: 4 ns at 250 MS/s on one channel.

That file (data/capture.parquet) was never committed - *.parquet is ignored -
so every published deck has rendered this chart empty. This regenerates it.

Needs Logic 2 running with the automation server enabled:
Logic 2 -> Preferences -> Automation -> "Enable automation server" (port 10430).

    python3 scripts/retake_staircase.py
    python3 scripts/analyse_dual.py      # folds it into deck_data.json
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from jnwtemp.acquire import SENSORS  # noqa: E402
from jnwtemp.board import TTBoard  # noqa: E402
from jnwtemp.edges import robust_stats  # noqa: E402
from jnwtemp.logic import CaptureSettings, LogicCapture  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "capture.parquet")
#: One channel only: the Logic Pro will do 250 MS/s on a single digital channel
#: and half that on two, and here resolution is the whole point.
CHANNEL = 0
RATE = 250_000_000


def automation_open(port=10430, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=1.0)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--port", default=None, help="board serial port")
    args = ap.parse_args()

    if not automation_open():
        raise SystemExit(
            "Logic 2's automation server is not listening on 10430.\n"
            "Logic 2 -> Preferences -> Automation -> Enable automation server.\n"
            "(The app being open and the probe being plugged in is not enough - "
            "an app update resets this preference.)")

    # GR07 free-runs, but only while the project is selected and clocked; a
    # capture of a dead pin would still 'succeed' and produce zero edges.
    b = TTBoard(args.port)
    b.connect()
    print("board:", b.select_project())
    hz = b.set_clock_hz(64_000_000)
    if hz != 64_000_000:
        b.disconnect()
        raise SystemExit(f"project clock came up at {hz}, expected 64 MHz")
    b.reset_project()
    b.disconnect()

    st = CaptureSettings(channels=[CHANNEL], sample_rate=RATE,
                         threshold_volts=1.2, duration_s=args.seconds)
    print(f"capturing {args.seconds:g} s on D{CHANNEL} at {RATE/1e6:.0f} MS/s …")
    with LogicCapture(st) as cap:
        trains = cap.capture()
        rate = cap.actual_sample_rate or RATE
        if cap.last_rate_note:
            print(" ", cap.last_rate_note)

    train = trains[CHANNEL]
    t, per = train.periods()
    if per.size < 1000:
        raise SystemExit(f"only {per.size} periods captured - is GR07 running?")

    kept, stats = robust_stats(per)
    ns = per * 1e9
    lsb = 1e9 / 64e6
    print(f"  {per.size:,} periods, mean {ns.mean():.2f} ns "
          f"({ns.mean()/lsb:.3f} clock cycles)")
    vals, counts = np.unique(np.round(ns, 1), return_counts=True)
    order = np.argsort(-counts)[:8]
    print(f"  {vals.size} distinct values; the busiest:")
    for i in sorted(order):
        print(f"    {vals[i]:9.1f} ns  {counts[i]:8,}  "
              f"({counts[i]/per.size*100:5.1f}%)")
    if vals.size > 200:
        print("  NOTE: that is a lot of distinct values for a retimed output - "
              "check the sample rate actually achieved above.")

    import pandas as pd
    df = pd.DataFrame({
        "t_s": t.astype(np.float64),
        "observable_s": per.astype(np.float64),
        "observable_ns": ns.astype(np.float64),
        "rate_hz": (1.0 / per).astype(np.float64),
    })
    df.to_parquet(args.out, index=False)
    print(f"\nwrote {os.path.relpath(args.out, os.path.dirname(HERE))} "
          f"({os.path.getsize(args.out)/1e6:.1f} MB, sample rate {rate/1e6:.0f} MS/s)")
    print("now run: python3 scripts/analyse_dual.py && "
          "python3 scripts/build_presentation.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
