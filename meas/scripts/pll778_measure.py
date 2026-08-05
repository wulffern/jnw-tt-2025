#!/usr/bin/env python3
"""Measure ttsky25a project 778 and write data/pll778_data.json for its deck.

    Design:  24 MHz MSSF PLL
    Author:  Nahwan Faza Assaify
    Chip:    Tiny Tapeout ttsky25a, project 778
    Source:  https://tinytapeout.com/chips/ttsky25a/tt_um_assaify_mssf_pll

Not our design. Measured with the rig built for JNW-TEMP, which suits it because
every control and observation pin is digital: ui_in[3:0] selects the integer
divider, ui_in[7:4] trims the VCO for process variation, and the outputs come
out on uo_out[1:0]. The reference must be 20 MHz - the first attempt here used
10 and produced nonsense.

What is recorded is the VCO discrete trim and the output's phase noise. The
loop was not brought into lock in this setup, so nothing is claimed about lock
behaviour, loop bandwidth or the divider: a configuration we could not find is
not a property of somebody else's silicon.

    python3 scripts/pll778_measure.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from jnwtemp.board import TTBoard  # noqa: E402
from jnwtemp.logic import CaptureSettings, LogicCapture  # noqa: E402
from jnwtemp.spectrum import allan_deviation, phase_noise  # noqa: E402
from ring_measure import thin_db  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "pll778_data.json")
PROJECT, INDEX, AUTHOR = "tt_um_assaify_mssf_pll", 778, "Nahwan Faza Assaify"
REF_HZ = 20_000_000          # the datasheet is explicit about this
LF_CH = 1                    # uo_out[1], DFOUT_LF


def capture(rate, seconds, ch=LF_CH):
    st = CaptureSettings(channels=[ch], sample_rate=int(rate),
                         threshold_volts=1.2, duration_s=seconds)
    with LogicCapture(st) as cap:
        tr = cap.capture()
        got = cap.actual_sample_rate or rate
    t = tr[ch]
    if t.num_edges < 64:
        return None, None, got
    _, per = t.periods()
    return per, t.duty(), got


def setup(b):
    b.exec(f"tt.shuttle.{PROJECT}.enable()", timeout=30)
    b.exec("import machine\n"
           f"hz = {REF_HZ}\n"
           "if machine.freq() < 2*hz: machine.freq(2*hz)\n"
           "tt.clock_project_PWM(hz)\n", timeout=25)


def main() -> None:
    b = TTBoard()
    b.connect()
    setup(b)

    # ---- VCO discrete trim, ui_in[7:4], with the divider select held at 0
    trim = []
    for code in range(16):
        b.reset_project()
        b.exec(f"tt.ui_in.value = {code << 4}", timeout=20)
        time.sleep(0.25)
        per, duty, _ = capture(250e6, 0.02)
        if per is None:
            continue
        trim.append({"code": code, "f_hz": float(1 / per.mean()),
                     "duty": float(duty),
                     "jitter_ppm": float(per.std(ddof=1) / per.mean() * 1e6)})
        print(f"  code {code:2d}: {1/per.mean()/1e6:.5f} MHz")
    f = np.array([t["f_hz"] for t in trim])
    codes = np.array([t["code"] for t in trim], dtype=float)
    step = -np.diff(f)
    lsb = float(step.mean())
    dnl = step / lsb - 1.0
    inl = (f - np.polyval(np.polyfit(codes, f, 1), codes)) / lsb

    # ---- phase noise, measured twice so the timebase can be ruled out
    b.reset_project()
    b.exec("tt.ui_in.value = 0", timeout=20)
    time.sleep(0.3)
    b.disconnect()
    runs = {}
    for rate in (250e6, 125e6):
        per, duty, got = capture(rate, 1.0)
        fq, L, f0 = phase_noise(per, nperseg=32768)
        pf, pL = thin_db(fq[1:], np.asarray(L[1:]))
        taus, dev = allan_deviation(per / per.mean(), per.mean())
        good = np.isfinite(dev) & (dev > 0)
        tt_, dd = np.asarray(taus)[good], np.asarray(dev)[good]
        s = max(1, tt_.size // 40)
        runs[str(int(got))] = {
            "sample_rate_hz": int(got), "periods": int(per.size),
            "f0_hz": float(f0), "duty": float(duty),
            "phase_noise": [[round(a, 4), round(v, 2)] for a, v in zip(pf, pL)],
            "allan": [[round(float(t), 6), round(float(x) * 1e6, 3)]
                      for t, x in zip(tt_[::s], dd[::s])],
        }
        print(f"  phase noise at {got/1e6:.0f} MS/s: {per.size:,} periods")

    ks = sorted(runs)
    fa = {a: v for a, v in runs[ks[0]]["phase_noise"]}
    diffs = []
    for a, v in runs[ks[1]]["phase_noise"]:
        near = min(fa, key=lambda x: abs(x - a))
        if abs(near - a) / max(a, 1) < 0.05 and 300 <= a <= 2e4:
            diffs.append(v - fa[near])

    out = {
        "design": {"project": PROJECT, "index": INDEX, "author": AUTHOR,
                   "chip": "Tiny Tapeout ttsky25a",
                   "url": f"https://tinytapeout.com/chips/ttsky25a/{PROJECT}",
                   "note": "measured by us; the design is not ours"},
        "ref_clock_hz": REF_HZ,
        "trim": {
            "codes": trim,
            "range_hz": [float(f.min()), float(f.max())],
            "range_pct": float((f.max() - f.min()) / f.mean() * 100),
            "lsb_hz": lsb,
            "lsb_pct": float(lsb / f.mean() * 100),
            "monotonic": bool(np.all(np.diff(f) < 0)),
            "dnl_lsb": [float(dnl.min()), float(dnl.max())],
            "inl_lsb": [float(inl.min()), float(inl.max())],
            "dnl": [round(float(v), 4) for v in dnl],
            "inl": [round(float(v), 4) for v in inl],
        },
        "runs": runs,
        # The validated band is 300 Hz to 20 kHz. Below it, a 1 s record gives
        # the lowest bins too few averages. Above it the 125 MS/s trace flattens
        # against its own 8 ns timebase while the 250 MS/s one keeps falling -
        # an instrument floor, not the oscillator, so the two must diverge there
        # and agreement would be the surprise.
        "cross_check": {"band_hz": [300, 20000],
                        "max_abs_diff_db": float(np.max(np.abs(diffs))),
                        "median_abs_diff_db": float(np.median(np.abs(diffs))),
                        "points": len(diffs),
                        "note": ("above 20 kHz the coarser timebase reaches its "
                                 "own floor; trust the faster capture there")},
    }
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"\ntrim: {out['trim']['range_pct']:.1f}% range, "
          f"monotonic={out['trim']['monotonic']}, "
          f"DNL {dnl.min():+.2f}/{dnl.max():+.2f} LSB")
    print(f"cross-check 300 Hz-30 kHz: {out['cross_check']['max_abs_diff_db']:.1f} dB")
    print("wrote", os.path.relpath(OUT, os.path.dirname(HERE)))


if __name__ == "__main__":
    main()
