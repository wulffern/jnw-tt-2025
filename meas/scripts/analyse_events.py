#!/usr/bin/env python3
"""Find and characterise thermal events (cooling spray, finger touch) in a run.

The trace is sampled in bursts - one capture, then dead time while the next is
armed and exported - so any derivative must be taken *within* a capture. The
``capture`` column exists for exactly this; differentiating across a gap would
manufacture a spike at every boundary.

Usage:  python3 scripts/analyse_events.py [file.csv ...]
"""
from __future__ import annotations

import csv
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")

#: Target noise on dT/dt, in K/s. The window is sized to reach this, per
#: sensor, because the two differ hugely: at 1 ms bins GR07 carries ~70 mK per
#: point and GR06 ~310 mK.
#:
#: Note the criterion is on the *derivative*, not on the smoothed trace. For a
#: local straight-line fit over a window W with N points of noise sigma, the
#: slope uncertainty is sigma*sqrt(12/N)/W = sigma*sqrt(12*dt)/W^1.5, so it
#: falls as W^-1.5 - far faster than the trace noise falls. Sizing the window
#: for a quiet-looking trace leaves the derivative dominated by noise.
DERIV_TARGET_K_PER_S = 0.5
MIN_SMOOTH_S = 0.03
MAX_SMOOTH_S = 2.0


def load(path):
    rows = list(csv.DictReader(open(path)))
    if not rows:
        raise SystemExit(f"{path}: empty")
    t = np.array([float(r["t_rel_s"]) for r in rows])
    cap = np.array([int(r["capture"]) for r in rows])
    cols = [c for c in rows[0] if c.endswith("temp_c") and not c.endswith("sem_c")]
    series = {}
    for c in cols:
        key = c[:-7] if c != "temp_c" else "sensor"
        series[key.rstrip("_") or "sensor"] = np.array(
            [float(r[c]) if r[c] else np.nan for r in rows]
        )
    return t, cap, series


def point_noise(v):
    """Robust per-point noise, from successive differences within a burst."""
    d = np.diff(v[np.isfinite(v)])
    if d.size < 8:
        return float("nan")
    return float(np.median(np.abs(d - np.median(d))) * 1.4826 / np.sqrt(2))


def choose_window(v, dt, target=DERIV_TARGET_K_PER_S):
    """Fit window giving a slope uncertainty of about ``target`` K/s.

    The closed form is only a starting guess - it assumes white noise and a
    straight-line fit, while the real trace has correlated content and the fit
    is quadratic. The window is therefore grown until the *measured* derivative
    noise meets the target, so the answer does not depend on that assumption
    holding.
    """
    sigma = point_noise(v)
    if not np.isfinite(sigma) or sigma <= 0:
        return MIN_SMOOTH_S
    w = (sigma * np.sqrt(12.0 * dt) / target) ** (2.0 / 3.0)
    return float(np.clip(w, MIN_SMOOTH_S, MAX_SMOOTH_S))


def tune_window(t, cap, v, target=DERIV_TARGET_K_PER_S, tries=6):
    """Grow the window until the measured dT/dt noise meets ``target``."""
    dt = float(np.median(np.diff(t[cap == cap[0]])))
    w = choose_window(v, dt, target)
    for _ in range(tries):
        _, _, d = smooth_and_diff(t, cap, v, window_s=w)
        d = d[np.isfinite(d)]
        if d.size < 32:
            break
        noise = float(np.median(np.abs(d - np.median(d))) * 1.4826)
        if noise <= target or w >= MAX_SMOOTH_S:
            break
        w = min(MAX_SMOOTH_S, w * float((noise / target) ** (2.0 / 3.0)) * 1.1)
    return w


def smooth_and_diff(t, cap, v, window_s=None):
    """Smoothed temperature and dT/dt, computed per capture and stitched.

    Returns (t_out, temp_smooth, dTdt) with NaN inserted at capture boundaries
    so nothing is drawn or differentiated across the dead time.
    """
    ts, vs, ds = [], [], []
    for c in np.unique(cap):
        m = cap == c
        tc, vc = t[m], v[m]
        good = np.isfinite(vc)
        tc, vc = tc[good], vc[good]
        if tc.size < 8:
            continue
        dt = np.median(np.diff(tc))
        w = window_s if window_s else choose_window(v, dt)
        n = max(3, int(round(w / dt)) | 1)   # odd window
        if vc.size <= n:
            continue
        # Local quadratic fit: gives a smoothed value and its slope in one
        # pass, and unlike a boxcar-then-difference it does not flatten the
        # peak rate of a fast transient.
        from scipy.signal import savgol_filter
        order = 2 if n > 5 else 1
        sm = savgol_filter(vc, n, order)
        d = savgol_filter(vc, n, order, deriv=1, delta=dt)
        tm = tc
        # savgol extrapolates a polynomial over the first and last half-window,
        # which on noisy data invents huge slopes exactly at capture edges. Those
        # points are not supported by a full window, so drop them: otherwise a
        # perfectly flat boundary reports tens of K/s.
        half = n // 2
        if sm.size > 2 * half:
            tm, sm, d = tm[half:-half], sm[half:-half], d[half:-half]
        else:
            continue
        ts.append(tm); vs.append(sm); ds.append(d)
        ts.append([np.nan]); vs.append([np.nan]); ds.append([np.nan])
    if not ts:
        return np.empty(0), np.empty(0), np.empty(0)
    return (np.concatenate(ts), np.concatenate(vs), np.concatenate(ds))


def find_events(t, temp, dtdt, min_rate=None, min_gap=8.0, n_sigma=8.0):
    """Locate excursions as local extrema of |dT/dt|.

    The threshold is derived from the record's own derivative noise rather than
    fixed, so a noisy sensor does not report its noise as events.
    """
    ok = np.isfinite(dtdt)
    if min_rate is None:
        d = dtdt[ok]
        noise = float(np.median(np.abs(d - np.median(d))) * 1.4826)
        min_rate = max(n_sigma * noise, 0.3)
    idx = np.flatnonzero(ok & (np.abs(dtdt) > min_rate))
    events = []
    for i in idx:
        if events and t[i] - events[-1]["t"] < min_gap:
            e = events[-1]
            if abs(dtdt[i]) > abs(e["rate"]):
                e.update(t=float(t[i]), rate=float(dtdt[i]), temp=float(temp[i]))
            continue
        events.append({"t": float(t[i]), "rate": float(dtdt[i]), "temp": float(temp[i])})
    return events


def describe(path):
    t, cap, series = load(path)
    out = {"file": os.path.basename(path), "span_s": float(t[-1]), "sensors": {}}
    for key, v in series.items():
        ts, sm, d = smooth_and_diff(t, cap, v)
        fin = np.isfinite(sm)
        ev = find_events(ts, sm, d)
        dt_bin = float(np.median(np.diff(t[cap == cap[0]])))
        out["sensors"][key] = {
            "point_noise_mk": point_noise(v) * 1000,
            "smooth_window_s": choose_window(v, dt_bin),
            "baseline_c": float(np.nanmedian(sm[ts < 10])) if (ts < 10).any() else float("nan"),
            "min_c": float(np.nanmin(sm)), "max_c": float(np.nanmax(sm)),
            "max_cool_k_per_s": float(np.nanmin(d)), "max_warm_k_per_s": float(np.nanmax(d)),
            "events": ev,
            "n_points": int(fin.sum()),
        }
    return out, t, cap, series


if __name__ == "__main__":
    files = sys.argv[1:] or sorted(
        os.path.join(DATA, f) for f in os.listdir(DATA)
        if f.startswith("jnwtemp-") and f.endswith(".csv")
    )
    for path in files:
        info, *_ = describe(path)
        print(f"\n=== {info['file']}  ({info['span_s']:.0f} s) ===")
        for key, s in info["sensors"].items():
            print(f"  {key}: baseline {s['baseline_c']:.2f} C, "
                  f"range {s['min_c']:.2f}..{s['max_c']:.2f} C")
            print(f"        fastest cooling {s['max_cool_k_per_s']:+.2f} K/s, "
                  f"fastest warming {s['max_warm_k_per_s']:+.2f} K/s")
            for e in s["events"]:
                print(f"        t={e['t']:6.1f}s  {e['rate']:+6.2f} K/s  at {e['temp']:5.2f} C")
