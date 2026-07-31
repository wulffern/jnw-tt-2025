#!/usr/bin/env python3
"""Reduce the stimulus run into the JSON the presentation's event slides use.

Emits the full trace, zoomed cutouts around each event, the derivative, the
sensor-to-sensor correlation per phase, and the dead-zone table.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyse_events import load, smooth_and_diff, tune_window, point_noise  # noqa: E402

DATA = os.path.join(HERE, "..", "data")
RUN = os.path.join(DATA, "jnwtemp-BOTH-20260731-213248.csv")
OUT = os.path.join(DATA, "event_data.json")
CLK_HZ = 64e6

#: Cutouts to show, as (label, t_from, t_to).
CUTOUTS = [
    ("Cooling spray", 11.5, 21.0),
    ("Finger on", 85.0, 95.0),
    ("Finger off", 119.0, 128.0),
]


def thin(t, v, keep=900):
    """Downsample for the page, preserving NaN gaps."""
    if t.size <= keep:
        return t, v
    step = int(np.ceil(t.size / keep))
    return t[::step], v[::step]


def main() -> None:
    t, cap, series = load(RUN)
    out = {"file": os.path.basename(RUN), "span_s": float(t[-1]), "sensors": {}}
    sm_all = {}

    for key, v in series.items():
        w = tune_window(t, cap, v)
        ts, sm, d = smooth_and_diff(t, cap, v, window_s=w)
        sm_all[key] = (ts, sm, d)
        quiet = (ts > 150) & (ts < 180) & np.isfinite(d)
        tt, vv = thin(ts, sm)
        out["sensors"][key] = {
            "window_ms": w * 1000,
            "deriv_noise_k_per_s": float(np.std(d[quiet])) if quiet.any() else None,
            "baseline_c": float(np.nanmedian(sm[ts < 12])),
            "min_c": float(np.nanmin(sm)),
            "max_c": float(np.nanmax(sm)),
            "peak_cool_k_per_s": float(np.nanmin(d)),
            "peak_warm_k_per_s": float(np.nanmax(d)),
            "trace": [[round(float(a), 3), None if not np.isfinite(b) else round(float(b), 3)]
                      for a, b in zip(tt, vv)],
        }

    # --- cutouts, both sensors plus GR07's derivative
    cuts = []
    for label, lo, hi in CUTOUTS:
        entry = {"label": label, "t0": lo, "t1": hi, "series": {}}
        for key, (ts, sm, d) in sm_all.items():
            m = (ts >= lo) & (ts <= hi)
            tt, vv = thin(ts[m], sm[m], keep=700)
            entry["series"][key] = [
                [round(float(a), 3), None if not np.isfinite(b) else round(float(b), 3)]
                for a, b in zip(tt, vv)
            ]
        ts, sm, d = sm_all["GR07"]
        m = (ts >= lo) & (ts <= hi)
        tt, dd = thin(ts[m], d[m], keep=700)
        entry["deriv_GR07"] = [
            [round(float(a), 3), None if not np.isfinite(b) else round(float(b), 2)]
            for a, b in zip(tt, dd)
        ]
        mm = m & np.isfinite(d)
        if mm.any():
            entry["peak_rate"] = float(d[mm][np.argmax(np.abs(d[mm]))])
        cuts.append(entry)
    out["cutouts"] = cuts

    # --- how fast, and how far
    ts, sm, d = sm_all["GR07"]
    base = out["sensors"]["GR07"]["baseline_c"]
    i_lo = int(np.nanargmin(sm))
    # Onset = the last moment still at baseline before the plunge. Searching for
    # the first steep slope anywhere finds start-up transients instead.
    near_base = np.flatnonzero(
        np.isfinite(sm) & (np.arange(sm.size) < i_lo) & (sm > base - 0.25)
    )
    onset = float(ts[near_base[-1]]) if near_base.size else float(ts[0])
    out["spray"] = {
        "onset_s": onset,
        "min_s": float(ts[i_lo]),
        "fall_s": float(ts[i_lo] - onset),
        "depth_k": float(np.nanmin(sm) - base),
        "mean_rate_k_per_s": float((np.nanmin(sm) - base) / (ts[i_lo] - onset)),
    }

    # --- correlation per phase: the discriminator between a real thermal event
    #     and a sensor artefact, since only the former moves both sensors.
    a, b = sm_all["GR07"], sm_all["GR06"]
    # The two smoothed series do not share a time base: each sensor gets its own
    # window, which trims a different amount at every capture edge. Put GR06 on
    # GR07's grid before correlating, and only where both are finite.
    ta, va = a[0], a[1]
    fb = np.isfinite(b[0]) & np.isfinite(b[1])
    vb_on_a = np.interp(ta, b[0][fb], b[1][fb], left=np.nan, right=np.nan)
    phases = {"spray + recovery (13-86 s)": (13, 86),
              "finger on (88-125 s)": (88, 125),
              "quiet tail (130-180 s)": (130, 180)}
    out["correlation"] = {}
    for name, (lo, hi) in phases.items():
        m = (ta > lo) & (ta < hi) & np.isfinite(va) & np.isfinite(vb_on_a)
        out["correlation"][name] = float(np.corrcoef(va[m], vb_on_a[m])[0, 1])
        out["correlation"][name + "_n"] = int(m.sum())

    # --- dead zone: noise vs where the period sits between clock edges
    import csv
    rows = list(csv.DictReader(open(RUN)))
    rate = np.array([float(r["GR07_rate_hz"]) for r in rows])
    temp = np.array([float(r["GR07_temp_c"]) for r in rows])
    zones = []
    for lo, hi in [(10, 12.5), (23, 26), (40, 60), (100, 115), (150, 180)]:
        m = (t > lo) & (t < hi)
        c0 = cap[m][0]
        mm = m & (cap == c0)
        per_ns = 1e9 / rate[mm].mean()
        cycles = per_ns / (1e9 / CLK_HZ)
        zones.append({
            "temp_c": float(np.median(temp[mm])),
            "sigma_mk": float(np.std(np.diff(temp[mm])) / np.sqrt(2) * 1000),
            "cycles": float(cycles),
            "frac": float(cycles % 1),
        })
    out["dead_zone"] = sorted(zones, key=lambda z: z["frac"])

    # --- the breath run: small, fast excursions -----------------------------
    BREATH = os.path.join(DATA, "jnwtemp-BOTH-20260731-223642.csv")
    if os.path.exists(BREATH):
        bt, bcap, bser = load(BREATH)
        bsm = {}
        for key, v in bser.items():
            bw = tune_window(bt, bcap, v)
            bsm[key] = smooth_and_diff(bt, bcap, v, window_s=bw) + (bw,)
        ta, va = bsm["GR07"][0], bsm["GR07"][1]
        fb = np.isfinite(bsm["GR06"][0]) & np.isfinite(bsm["GR06"][1])
        vb = np.interp(ta, bsm["GR06"][0][fb], bsm["GR06"][1][fb],
                       left=np.nan, right=np.nan)
        ok = np.isfinite(va) & np.isfinite(vb) & np.isfinite(ta)
        base_a = float(np.median(va[ok & (ta < 12)]))
        base_b = float(np.median(vb[ok & (ta < 12)]))

        peaks = []
        for lo, hi in [(12, 20), (33, 50), (55, 72), (74, 95)]:
            m = ok & (ta >= lo) & (ta < hi)
            if m.sum() < 50:
                continue
            da = float(np.max(va[m]) - base_a)
            db = float(np.max(vb[m]) - base_b)
            peaks.append({"t": float(ta[m][int(np.argmax(va[m]))]),
                          "gr07": da, "gr06": db, "ratio": db / da})

        # Where GR07 sat between clock edges, and what its noise did there.
        import csv as _csv
        rows = list(_csv.DictReader(open(BREATH)))
        brate = np.array([float(r["GR07_rate_hz"]) for r in rows])
        bT7 = np.array([float(r["GR07_temp_c"]) for r in rows])
        bT6 = np.array([float(r["GR06_temp_c"]) for r in rows])
        zones = []
        for lo, hi, lab in [(0, 12, "baseline"), (35, 38, "breath peak"),
                            (45, 55, "decay"), (105, 115, "tail")]:
            m = (bt >= lo) & (bt < hi)
            c0 = bcap[m][0]
            mm = m & (bcap == c0)
            per = 1e9 / brate[mm].mean()
            cyc = per / (1e9 / CLK_HZ)
            zones.append({
                "label": lab, "temp_c": float(np.median(bT7[mm])),
                "cycles": float(cyc), "to_edge": float(min(cyc % 1, 1 - cyc % 1)),
                "sigma_mk": float(np.std(np.diff(bT7[mm])) / np.sqrt(2) * 1000),
                "sigma6_mk": float(np.std(np.diff(bT6[mm])) / np.sqrt(2) * 1000),
            })

        tt, aa = thin(ta, va, keep=800)
        _, bb2 = thin(ta, vb, keep=800)
        out["breath"] = {
            "span_s": float(np.nanmax(ta)),
            "baseline": {"GR07": base_a, "GR06": base_b},
            "peaks": peaks,
            "median_ratio": float(np.median([p["ratio"] for p in peaks])),
            "zones": zones,
            "trace": {
                "GR07": [[round(float(x), 3), None if not np.isfinite(y) else round(float(y), 3)]
                         for x, y in zip(tt, aa)],
                "GR06": [[round(float(x), 3), None if not np.isfinite(y) else round(float(y), 3)]
                         for x, y in zip(tt, bb2)],
            },
        }

        # Same-gain check against the wide excursion of the stimulus run.
        a2, b2 = sm_all["GR07"], sm_all["GR06"]
        t2 = a2[0]
        f2 = np.isfinite(b2[0]) & np.isfinite(b2[1])
        v2b = np.interp(t2, b2[0][f2], b2[1][f2], left=np.nan, right=np.nan)
        g = np.isfinite(a2[1]) & np.isfinite(v2b)
        A, B = a2[1][g] - 23.0, v2b[g] - 23.0
        out["breath"]["wide_slope"] = float(np.sum(A * B) / np.sum(A * A))
        out["breath"]["wide_r"] = float(np.corrcoef(A, B)[0, 1])

    json.dump(out, open(OUT, "w"), separators=(",", ":"))
    kb = os.path.getsize(OUT) / 1024
    print(f"wrote {OUT} ({kb:.0f} kB)")
    s = out["spray"]
    print(f"spray: onset {s['onset_s']:.2f}s, min at {s['min_s']:.2f}s, "
          f"{s['depth_k']:+.2f} K in {s['fall_s']:.2f}s (mean {s['mean_rate_k_per_s']:+.1f} K/s)")
    for k, v in out["sensors"].items():
        print(f"{k}: window {v['window_ms']:.0f} ms, peak {v['peak_cool_k_per_s']:+.1f} / "
              f"{v['peak_warm_k_per_s']:+.1f} K/s, noise {v['deriv_noise_k_per_s']:.2f} K/s")
    for k, v in out["correlation"].items():
        print(f"corr {k:28} {v:+.3f}")
    for c in out["cutouts"]:
        print(f"cutout {c['label']:14} peak {c.get('peak_rate', float('nan')):+7.2f} K/s")


if __name__ == "__main__":
    main()
