#!/usr/bin/env python3
"""Replay a recorded chamber log through the sweep's settling criterion.

The sweep decides when a setpoint has settled. Getting that wrong is expensive
and nearly invisible: the run still completes and the data still looks fine,
it just takes twice as long and some points never legitimately settle at all.
The only honest test is to replay a real log and count.

Against the 2026-08-03 sweep this reproduces the original problem and the fix:

    criterion                      settle time         never settled
    |actual-set| <= 0.3 K          230 -> 768 s         1 of 14
    drift <= 0.5 K/min             160 -> 193 s         0 of 14

The first grows with temperature because the chamber has a steady-state offset
below its setpoint that reaches -0.54 K at 70 degC, so the band is satisfied at
the bottom of the range and never at the top. The second is flat, because it
asks whether the chamber has stopped moving rather than where it stopped.

Usage:  python3 scripts/chamber_settle_check.py [run.csv]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
from chamber_analyse import RUN                              # noqa: E402
from jnwtemp.chamber_worker import SweepPlan                 # noqa: E402

POLL_S = 1.0
MIN_SEGMENT_S = 200.0


def _resample(g):
    """The 1 Hz view of a segment that the sweep thread would have seen."""
    t = g.t_rel_s.to_numpy()
    a = g.chamber_actual_c.to_numpy()
    grid = np.arange(t[0], t[-1], POLL_S)
    return grid, a[np.searchsorted(t, grid, side="right") - 1]


def settle_by_band(t, a, setpoint, tol_c, soak_s):
    """The old criterion: within tol of the setpoint, held continuously."""
    stable = None
    for i in range(len(t)):
        if abs(a[i] - setpoint) <= tol_c:
            if stable is None:
                stable = t[i]
            if t[i] - stable >= soak_s:
                return t[i] - t[0]
        else:
            stable = None                     # one stray sample resets it all
    return None


def settle_by_drift(t, a, drift_lim, soak_s):
    """The current criterion: the reading has stopped changing."""
    for i in range(len(t)):
        lo = np.searchsorted(t, t[i] - soak_s)
        if t[i] - t[lo] < soak_s * 0.95:
            continue
        if abs(np.polyfit(t[lo:i + 1], a[lo:i + 1], 1)[0]) * 60 <= drift_lim:
            return t[i] - t[0]
    return None


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else RUN
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found - this needs a raw chamber log, "
                         "which is deliberately not committed")
    plan = SweepPlan(start_c=5, stop_c=70, step_c=5)
    soak = 60.0                               # what the 2026-08-03 run used
    d = pd.read_csv(path, usecols=["t_rel_s", "chamber_set_c", "chamber_actual_c"])
    d["seg"] = (d.chamber_set_c.diff().fillna(0) != 0).cumsum()

    print(f"{os.path.basename(path)}   soak {soak:g}s, "
          f"tol ±{plan.tol_c:g} K, drift ≤ {plan.drift_k_per_min:g} K/min\n")
    print(f"{'set':>6} {'offset':>8} {'by band':>9} {'by drift':>9}")
    band, drift = [], []
    for _, g in d.groupby("seg"):
        if g.t_rel_s.iloc[-1] - g.t_rel_s.iloc[0] < MIN_SEGMENT_S:
            continue
        t, a = _resample(g)
        sp = float(g.chamber_set_c.iloc[0])
        off = float(np.median(a[t > t[-1] - 120])) - sp
        b = settle_by_band(t, a, sp, plan.tol_c, soak)
        r = settle_by_drift(t, a, plan.drift_k_per_min, soak)
        band.append(b if b else np.nan)
        drift.append(r if r else np.nan)
        f = lambda v: f"{v:7.0f}s" if v else "  never"
        print(f"{sp:6.1f} {off:+8.2f} {f(b):>9} {f(r):>9}")

    band, drift = np.array(band, float), np.array(drift, float)
    print(f"\n{'':>14}{'by band':>12}{'by drift':>12}")
    print(f"{'never settled':>14}{np.isnan(band).sum():12d}{np.isnan(drift).sum():12d}")
    print(f"{'median':>14}{np.nanmedian(band):11.0f}s{np.nanmedian(drift):11.0f}s")
    print(f"{'spread':>14}{np.nanstd(band):11.0f}s{np.nanstd(drift):11.0f}s")
    print(f"{'total':>14}{np.nansum(band)/60:10.0f}m{np.nansum(drift)/60:10.0f}m")

    ok = np.isnan(drift).sum() == 0 and np.nanstd(drift) < np.nanstd(band)
    print("\n" + ("PASS - every point settles, and the settle time no longer "
                  "grows with temperature" if ok else
                  "FAIL - the drift criterion is not behaving"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
