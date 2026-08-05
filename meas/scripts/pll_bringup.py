#!/usr/bin/env python3
"""Bring up and measure tt_um_tiny_pll (ttsky25a project 128).

    Design:  Tiny PLL - a fractional-N frequency synthesiser
    Author:  LegumeEmittingDiode
    Chip:    Tiny Tapeout ttsky25a, project 128

Not our design. This is a measurement of somebody else's silicon, made because
it is analog at the core - VCO, charge pump, loop filter - while every pin is
digital, so the rig built for JNW-TEMP applies to it unchanged and without any
extra wiring.

Programming, from the project's documentation:

    ui_in[3:0]   CSR data          ui_in[7:4]   CSR address
    uio_in[0]    CSR clock, latches on the rising edge
    rst_n        ~50 ns, resets all CSRs and disables every channel

    0x0-0x7   feedback and output divider ratios, 4 bits each, 1..15
    0x8       enb - active LOW enable for the four channels
    0x9-0xC   per-channel clock source select

    f_out = (A / B) * f_ref,  A = feedback divider, B = output divider

The docs do not say which of 0x0-0x7 is feedback and which is output, so this
does not guess: it writes an asymmetric pair, measures the frequency, and lets
the ratio identify the mapping.

    python3 scripts/pll_bringup.py
    python3 scripts/pll_bringup.py --restore     # put project 258 back
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from jnwtemp.board import TTBoard  # noqa: E402
from jnwtemp.logic import CaptureSettings, LogicCapture  # noqa: E402

PROJECT = "tt_um_tiny_pll"
INDEX = 128
AUTHOR = "LegumeEmittingDiode"
F_REF = 10_000_000

#: One CSR write. ui_in carries address and data together; the rising edge of
#: uio_in[0] latches it. The pin is driven through raw_pin because the SDK's
#: wrapper costs ~15 ms per write and 13 registers would take a fifth of a
#: second of dead time for no reason.
CSR_WRITE = """
import time
from machine import Pin
clk = tt.pins.uio0.raw_pin
clk.init(Pin.OUT)
clk.value(0)
def w(addr, data):
    tt.ui_in.value = ((addr & 0xF) << 4) | (data & 0xF)
    time.sleep_us(5)
    clk.value(1)
    time.sleep_us(5)
    clk.value(0)
    time.sleep_us(5)
"""


def program(b, writes, note=""):
    body = CSR_WRITE + "".join(f"w({a}, {d})\n" for a, d in writes)
    body += "print('wrote', %d, 'registers')\n" % len(writes)
    return b.exec(body, timeout=25)


def measure(channels=(0, 1, 2), seconds=0.02, rate=125_000_000):
    st = CaptureSettings(channels=list(channels), sample_rate=rate,
                         threshold_volts=1.2, duration_s=seconds)
    with LogicCapture(st) as cap:
        tr = cap.capture()
        got = cap.actual_sample_rate or rate
    out = {}
    for ch in sorted(tr):
        t = tr[ch]
        if t.num_edges < 8:
            out[ch] = None
            continue
        _, per = t.periods()
        out[ch] = {"f_hz": float(1.0 / per.mean()),
                   "edges": int(t.num_edges),
                   "duty": float(t.duty()),
                   "jitter_ppm": float(per.std(ddof=1) / per.mean() * 1e6),
                   "periods": per}
    return out, got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--port", default=None)
    args = ap.parse_args()

    b = TTBoard(args.port)
    b.connect()
    if args.restore:
        print(b.select_project())
        print("clock ->", b.set_clock_hz(64_000_000))
        b.reset_project()
        b.disconnect()
        return 0

    print(f"{PROJECT} ({INDEX}) by {AUTHOR}")
    out = b.exec(f"tt.shuttle.{PROJECT}.enable()\nprint(tt)\n", timeout=25)
    print(" ", out.strip().splitlines()[-1])
    b.exec("import machine\n"
           f"hz = {F_REF}\n"
           "if machine.freq() < 2*hz: machine.freq(2*hz)\n"
           "tt.clock_project_PWM(hz)\n"
           "print('ref', tt.auto_clocking_freq)\n", timeout=25)
    b.reset_project()

    # Asymmetric divider pair, so the measured ratio names the mapping.
    A, B = 8, 2
    print(f"\nwriting dividers {A} and {B} to 0x0/0x1, enabling channel 0 "
          f"(enb is active low)")
    program(b, [(0x0, A), (0x1, B), (0x8, 0x0)])

    res, got = measure()
    print(f"\ncaptured at {got/1e6:.0f} MS/s")
    live = False
    for ch, r in res.items():
        if r is None:
            print(f"  uo_out[{ch}]: static")
            continue
        live = True
        ratio = r["f_hz"] / F_REF
        print(f"  uo_out[{ch}]: {r['f_hz']/1e6:9.4f} MHz  "
              f"ratio {ratio:6.3f}  duty {r['duty']*100:5.1f}%  "
              f"period jitter {r['jitter_ppm']:.0f} ppm")
        if abs(ratio - A / B) < 0.05:
            print(f"      -> matches A/B = {A}/{B}: 0x0 is feedback, "
                  f"0x1 is output")
        elif abs(ratio - B / A) < 0.05:
            print(f"      -> matches B/A = {B}/{A}: 0x0 is output, "
                  f"0x1 is feedback")
    if not live:
        print("\n  nothing toggling. The CSR map or the enable polarity is not"
              "\n  what the docs imply; stopping rather than guessing further.")
    b.disconnect()
    return 0 if live else 1


if __name__ == "__main__":
    raise SystemExit(main())
