#!/usr/bin/env python3
"""Reduce a chamber run to a committable dwell-only dataset.

The raw run is ~78 MB, most of which is the chamber slewing between set points.
That part is not usable for characterisation anyway - the chamber's own probe
does not represent the die while the loop is moving, which shows up as a
sawtooth locked to the set-point spacing - so only the settled tail of each
dwell is kept.

Writes four files:

  <stem>_dwell.csv     every 10 ms sample from the last DWELL_TAIL_S of each
                       dwell. Supports transfer, INL, calibration, noise,
                       Allan and within-dwell spectra.
  <stem>_summary.csv   one row per dwell: the reference, both sensors' mean
                       rate, their noise, and where GR07 sat on the clock grid.
  <stem>_dnl.csv       GR07 code widths. Precomputed because DNL needs the
                       *sweep* between dwells to find code boundaries, and that
                       is exactly what this extraction throws away.
  <stem>_trace.csv     the whole run decimated to 1 s, ramps included. Far too
                       coarse for noise, but the settling story happens over
                       minutes, so this is all that figure ever needed.

Usage:  python3 scripts/chamber_extract.py [run.csv] [--tail 60]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from chamber_analyse import (  # noqa: E402
    CLK_HZ, DATA, KELVIN, RUN, SENSORS, SETTLE_TAIL_S,
    fit_affine, invert, load, settled_table,
)
from chamber_figures import code_boundaries  # noqa: E402

#: Seconds of settled tail kept per dwell. Must match the analysis window, or
#: the committed extract will not reproduce the published numbers - averaging a
#: different slice of the dwell shifts the mean by a few hundred mK.
DWELL_TAIL_S = SETTLE_TAIL_S

#: Bin for the whole-run context trace. The chamber's own loop moves over
#: minutes, so 1 s loses nothing that figure uses.
TRACE_BIN_S = 1.0

#: Columns kept per sample. chamber_set_c is dropped (constant within a dwell,
#: so it lives in the summary) and so is GR06_events (constant over the whole
#: run). Rates are written to 0.1 Hz: that is ~1600x below GR07's own
#: per-sample noise, so the digits below it are noise, and noise is precisely
#: what compresses worst.
KEEP = ["t_rel_s", "chamber_actual_c",
        "GR07_rate_hz", "GR07_events", "GR06_rate_hz", "capture"]


def extract_dwells(df, tail_s):
    """Rows from the settled tail of every dwell, with a dwell index."""
    out = []
    for seg, g in df.groupby("segment"):
        if g.t_rel_s.iloc[-1] - g.t_rel_s.iloc[0] < 120:
            continue
        t = g[g.t_rel_s > g.t_rel_s.iloc[-1] - tail_s].copy()
        if len(t) < 100:
            continue
        t["dwell"] = int(seg)
        out.append(t)
    d = pd.concat(out, ignore_index=True)
    # set_c and GR06_events are carried through for the summary, then dropped
    # at write time - both are constant where the dwell file would repeat them.
    return d[["dwell", "chamber_set_c", "GR06_events"] + KEEP]


def dnl_table(df, tab):
    """GR07 code widths, measured against GR06 during the sweep."""
    d = df[df.chamber_on == 1]
    code = np.round((1e9 / d.GR07_rate_hz.to_numpy()) / (1e9 / CLK_HZ))
    a6, b6 = fit_affine(tab.ref_c, tab.GR06_rate)
    ruler = invert(a6, b6, d.GR06_rate_hz.to_numpy())
    ok = np.isfinite(code) & np.isfinite(ruler)
    bounds = code_boundaries(code[ok], ruler[ok])
    ks = sorted(bounds)
    rows = []
    for lo, hi in zip(ks, ks[1:]):
        if hi - lo != 1:
            continue
        rows.append({"code": hi, "width_k": abs(bounds[lo] - bounds[hi])})
    t = pd.DataFrame(rows)
    if len(t) > 4:                      # outermost codes were only partly swept
        t = t.iloc[1:-1].reset_index(drop=True)
    t["lsb_k"] = t.width_k.median()
    t["dnl_lsb"] = t.width_k / t.lsb_k - 1.0
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?", default=RUN)
    ap.add_argument("--tail", type=float, default=DWELL_TAIL_S)
    args = ap.parse_args()

    print(f"reading {os.path.basename(args.run)} …")
    df = load(args.run)
    tab = settled_table(df)
    stem = os.path.join(DATA, "2026-08-03_chamber")

    # ---- dwell samples
    dw = extract_dwells(df, args.tail)
    fmt = {"t_rel_s": "%.2f", "chamber_actual_c": "%.2f",
           "GR07_rate_hz": "%.1f", "GR06_rate_hz": "%.1f"}
    for c, f in fmt.items():
        dw[c] = dw[c].map(lambda v, f=f: f % v)
    p = f"{stem}_dwell.csv"
    dw[["dwell"] + KEEP].to_csv(p, index=False)
    print(f"  {os.path.basename(p):34} {len(dw):7,} rows  "
          f"{os.path.getsize(p)/1e6:5.2f} MB")

    # ---- per-dwell summary, computed from the rows actually written above so
    #      the two files agree by construction
    tab = pd.DataFrame([
        {"set_c": g.chamber_set_c.astype(float).iloc[0],
         "ref_c": g.chamber_actual_c.astype(float).median(),
         "ref_spread": g.chamber_actual_c.astype(float).std(),
         "n": len(g),
         **{f"{s_}_rate": g[f"{s_}_rate_hz"].astype(float).mean() for s_ in SENSORS},
         **{f"{s_}_sigma_hz": float(np.std(np.diff(g[f"{s_}_rate_hz"].astype(float)))
                                    / np.sqrt(2)) for s_ in SENSORS},
         **{f"{s_}_events": float(g[f"{s_}_events"].median()) for s_ in SENSORS}}
        for _, g in dw.groupby("dwell")
    ]).sort_values("ref_c").reset_index(drop=True)

    per = 1e9 / tab.GR07_rate.to_numpy()
    cyc = per / (1e9 / CLK_HZ)
    s = pd.DataFrame({
        "set_c": tab.set_c, "ref_c": tab.ref_c, "ref_std_c": tab.ref_spread,
        "n_samples": tab.n,
        "GR07_rate_hz": tab.GR07_rate, "GR07_sigma_hz": tab.GR07_sigma_hz,
        "GR07_events_per_bin": tab.GR07_events,
        "GR07_clock_cycles": cyc,
        "GR07_dist_to_whole_cycle": np.minimum(cyc % 1, 1 - cyc % 1),
        "GR06_rate_hz": tab.GR06_rate, "GR06_sigma_hz": tab.GR06_sigma_hz,
        "GR06_events_per_bin": tab.GR06_events,
    })
    p = f"{stem}_summary.csv"
    s.to_csv(p, index=False, float_format="%.4f")
    print(f"  {os.path.basename(p):34} {len(s):7,} rows  "
          f"{os.path.getsize(p)/1e3:5.1f} kB")

    # ---- 1 s trace of the whole run, so the settling figure does not need
    #      the 78 MB original either
    tr = df.copy()
    tr["bucket"] = (tr.t_rel_s // TRACE_BIN_S).astype(int)
    trace = tr.groupby("bucket").agg(
        t_rel_s=("t_rel_s", "mean"),
        chamber_set_c=("chamber_set_c", "median"),
        chamber_actual_c=("chamber_actual_c", "mean"),
        chamber_on=("chamber_on", "max"),
        GR07_rate_hz=("GR07_rate_hz", "mean"),
        GR06_rate_hz=("GR06_rate_hz", "mean"),
    ).reset_index(drop=True)
    p = f"{stem}_trace.csv"
    trace.to_csv(p, index=False, float_format="%.3f")
    print(f"  {os.path.basename(p):34} {len(trace):7,} rows  "
          f"{os.path.getsize(p)/1e3:5.1f} kB")

    # ---- DNL (needs the sweep, so it is computed now and kept)
    dnl = dnl_table(df, tab)   # tab now comes from the extract; same set points
    p = f"{stem}_dnl.csv"
    dnl.to_csv(p, index=False, float_format="%.4f")
    print(f"  {os.path.basename(p):34} {len(dnl):7,} rows  "
          f"{os.path.getsize(p)/1e3:5.1f} kB")

    # ---- provenance carried over from the original run
    meta_src = os.path.splitext(args.run)[0] + ".meta.json"
    meta = json.load(open(meta_src)) if os.path.exists(meta_src) else {}
    meta["derived"] = {
        "from": os.path.basename(args.run),
        "tool": "scripts/chamber_extract.py",
        "dwell_tail_s": args.tail,
        "note": ("Only the settled tail of each dwell is kept. Data recorded "
                 "while the chamber was slewing is excluded: its own probe does "
                 "not represent the die then, which appears as a sawtooth locked "
                 "to the set-point spacing. DNL needs those sweeps to find code "
                 "boundaries, so it is precomputed into the _dnl.csv."),
        "dwells": int(dw.dwell.nunique()),
        "rate_resolution_hz": 0.1,
        "constant_columns": {"GR06_events_per_10ms_bin": 45},
        "reference_range_c": [float(tab.ref_c.min()), float(tab.ref_c.max())],
    }
    p = f"{stem}.meta.json"
    json.dump(meta, open(p, "w"), indent=2)
    print(f"  {os.path.basename(p):34}          {os.path.getsize(p)/1e3:5.1f} kB")


if __name__ == "__main__":
    main()
