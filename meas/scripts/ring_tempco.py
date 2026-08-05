#!/usr/bin/env python3
"""Temperature coefficient of the ring oscillator, without a climate chamber.

    Ring:        Giant Ring Oscillator, ttsky25a project 132, by Uri Shaked
    Thermometer: JNW-TEMP GR07, ttsky25a project 258 (ours)

There is no chamber here, which is why the ring deck has no tempco. But the
same die carries a temperature sensor we already calibrated against a chamber
on 2026-08-03, so it can serve as the thermometer: switch the mux to project
258, read the die temperature, switch back, read the ring. Ambient drift over
an hour supplies the temperature excursion for free.

Two things make this workable rather than circular. The sensor's calibration is
external - it came from a Votsch chamber, not from anything on this die - and
a tempco is a *slope*, so GR07's ~1.6 K absolute accuracy does not enter; only
its differential response does, and that is good to a few millikelvin.

What it cannot do is measure both at once. The mux carries one project at a
time, so ring and temperature are interleaved and each pairing assumes the die
does not move much in the ~10 s between them. The ring dissipates about 40 uW,
so switching it off and on shifts the die by well under a tenth of a kelvin -
small against the ambient drift this relies on, but not zero, and it is the
main systematic here.

    python3 scripts/ring_tempco.py --minutes 45
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from board_only_probe import GR07_CODE, SENSOR_GPIO, TICK_GPIO, run  # noqa: E402
from chamber_analyse import KELVIN, fit_affine  # noqa: E402
from jnwtemp.board import TTBoard  # noqa: E402
from jnwtemp.logic import CaptureSettings, LogicCapture  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
RING, RING_INDEX, RING_AUTHOR = "tt_um_urish_giant_ringosc", 132, "Uri Shaked"
OURS = "tt_um_jnw_wulffern"
RING_REF_HZ = 10_000_000
OURS_CLOCK_HZ = 64_000_000
F_RING, RING_PERIODS = 2.948e6, 2948
F_GR07, GR07_PERIODS = 0.912e6, 907


def calibration():
    """GR07 rate -> degrees C, from the chamber sweep. External to this die."""
    t = pd.read_csv(os.path.join(DATA, "2026-08-03_chamber_summary.csv"))
    a, b = fit_affine(t.ref_c, t.GR07_rate_hz)
    return a, b


def count(b, sysclk, gpio, periods, n):
    code = (GR07_CODE.replace("SYSCLK_HZ", str(sysclk))
            .replace("PERIODS_M1", str(periods - 1)).replace("NBINS_N", str(n))
            .replace("TIMEOUT_US", "8000000")
            .replace("SENSOR_GPIO", str(gpio)).replace("TICK_GPIO", str(TICK_GPIO)))
    ticks, el = run(b, code, timeout=40)
    return periods / (ticks.astype(float) * 3 / sysclk)


def start_ring(b, tries=12):
    for _ in range(tries):
        b.reset_project()
        b.exec("tt.ui_in.value = 0x00", timeout=20)
        b.reset_project()
        b.exec("tt.ui_in.value = 0x02", timeout=20)
        time.sleep(0.2)
        st = CaptureSettings(channels=[0], sample_rate=250_000_000,
                             threshold_volts=1.2, duration_s=0.004)
        with LogicCapture(st) as cap:
            tr = cap.capture()
        if tr[0].num_edges < 32:
            continue
        _, per = tr[0].periods()
        if abs(1 / per.mean() - F_RING) / F_RING < 0.05:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=45.0)
    ap.add_argument("--samples", type=int, default=3000,
                    help="counter samples per visit (~1 ms each)")
    ap.add_argument("--port", default=None)
    args = ap.parse_args()

    a_cal, b_cal = calibration()
    print(f"thermometer: GR07 at {a_cal:.1f} Hz/K, calibrated 2026-08-03\n")

    b = TTBoard(args.port)
    b.connect()
    sysclk = int(b.exec_eval("__import__('machine').freq()"))
    rows = []
    t0 = time.time()
    deadline = t0 + args.minutes * 60
    print(f"{'elapsed':>9} {'ring MHz':>11} {'GR07 kHz':>10} {'die C':>8} "
          f"{'ring ppm':>9}")
    f_ref = None
    try:
        while time.time() < deadline:
            # --- the ring
            b.exec(f"tt.shuttle.{RING}.enable()", timeout=30)
            b.exec("import machine\n"
                   f"hz = {RING_REF_HZ}\n"
                   "if machine.freq() < 2*hz: machine.freq(2*hz)\n"
                   "tt.clock_project_PWM(hz)\n", timeout=25)
            if not start_ring(b):
                print("  ring did not start; skipping this cycle")
                continue
            sysclk = int(b.exec_eval("__import__('machine').freq()"))
            fr = count(b, sysclk, SENSOR_GPIO, RING_PERIODS, args.samples)
            t_ring = time.time()

            # --- our thermometer
            b.exec(f"tt.shuttle.{OURS}.enable()", timeout=30)
            b.exec("import machine\n"
                   f"hz = {OURS_CLOCK_HZ}\n"
                   "if machine.freq() < 2*hz: machine.freq(2*hz)\n"
                   "tt.clock_project_PWM(hz)\n", timeout=25)
            b.reset_project()
            sysclk = int(b.exec_eval("__import__('machine').freq()"))
            fg = count(b, sysclk, SENSOR_GPIO, GR07_PERIODS, args.samples)
            t_temp = time.time()

            ring_hz = float(np.median(fr))
            gr07_hz = float(np.median(fg))
            die_c = (gr07_hz - b_cal) / a_cal - KELVIN
            if f_ref is None:
                f_ref = ring_hz
            rows.append({"t": t_ring - t0, "gap_s": t_temp - t_ring,
                         "ring_hz": ring_hz, "gr07_hz": gr07_hz,
                         "die_c": die_c,
                         "ring_sd_ppm": float(fr.std(ddof=1) / fr.mean() * 1e6),
                         "gr07_sd_ppm": float(fg.std(ddof=1) / fg.mean() * 1e6)})
            print(f"{(t_ring-t0)/60:8.2f}m {ring_hz/1e6:11.6f} "
                  f"{gr07_hz/1e3:10.3f} {die_c:8.3f} "
                  f"{(ring_hz-f_ref)/f_ref*1e6:+9.0f}")
    except KeyboardInterrupt:
        print("\ninterrupted - keeping what was measured")
    finally:
        b.exec(f"tt.shuttle.{OURS}.enable()", timeout=30)
        b.exec("import machine\n"
               f"hz = {OURS_CLOCK_HZ}\n"
               "if machine.freq() < 2*hz: machine.freq(2*hz)\n"
               "tt.clock_project_PWM(hz)\n", timeout=25)
        b.disconnect()

    if len(rows) < 6:
        raise SystemExit("too few cycles to fit a slope")
    d = pd.DataFrame(rows)
    frac = (d.ring_hz / d.ring_hz.mean() - 1) * 1e6
    slope, icept = np.polyfit(d.die_c, frac, 1)
    r = float(np.corrcoef(d.die_c, frac)[0, 1])
    span = float(d.die_c.max() - d.die_c.min())
    print(f"\n{len(d)} cycles over {d.t.iloc[-1]/60:.1f} min")
    print(f"  die temperature spanned {span:.3f} K "
          f"({d.die_c.min():.2f} to {d.die_c.max():.2f} C)")
    print(f"  ring tempco {slope:+.0f} ppm/K   r = {r:+.3f}")
    if span < 0.5:
        print("  NOTE: that span is small; the slope is poorly conditioned and")
        print("        should be treated as indicative, not a measurement.")

    stem = os.path.join(DATA, f"ring-tempco-{time.strftime('%Y%m%d-%H%M%S')}")
    d.to_csv(stem + ".csv", index=False)
    json.dump({"ring": {"project": RING, "index": RING_INDEX,
                        "author": RING_AUTHOR,
                        "note": "measured by us; the design is not ours"},
               "thermometer": {"project": OURS, "sensor": "GR07",
                               "cal_hz_per_k": a_cal, "cal_intercept_hz": b_cal,
                               "cal_source": "2026-08-03 Votsch chamber sweep"},
               "cycles": len(d), "span_k": span,
               "tempco_ppm_per_k": float(slope), "r": r,
               "interleave_gap_s": float(d.gap_s.median())},
              open(stem + ".meta.json", "w"), indent=1)
    print("wrote", os.path.basename(stem) + ".csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
