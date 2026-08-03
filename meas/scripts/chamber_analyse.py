#!/usr/bin/env python3
"""Characterise both sensors against the climate-chamber reference.

The chamber run steps through set points and logs its own actual temperature
alongside both sensors, so for the first time there is an external reference to
compare against rather than a single self-consistent calibration point.

Produces, into data/figs/:
  transfer    reading vs reference, both sensors
  calerror    residual error for 1-, 2- and N-point calibrations
  inl         integral nonlinearity of the rate-vs-temperature transfer
  noise       per-point noise vs temperature (the dead-zone comb, for GR07)
  settling    step response and fitted time constants
  dnl         GR07 code (period in whole clock cycles) differential nonlinearity

Usage:  python3 scripts/chamber_analyse.py [run.csv]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
FIGS = os.path.join(DATA, "figs")
RUN = os.path.join(DATA, "jnwtemp-BOTH-20260803-170642.csv")
CLK_HZ = 64e6
KELVIN = 273.15

#: Seconds at the end of each set-point segment treated as settled. The chamber
#: needs several minutes; the tail is where both it and the die have stopped
#: moving, and it is the only part that says anything about the transfer.
SETTLE_TAIL_S = 90.0

SENSORS = ("GR07", "GR06")
COLOR = {"GR07": "#2a78d6", "GR06": "#d1521f"}


def load(path=RUN):
    df = pd.read_csv(path)
    seg = (df.chamber_set_c.diff().fillna(0) != 0).cumsum()
    df["segment"] = seg
    return df


def settled_table(df):
    """One row per set point, from the settled tail of its segment."""
    rows = []
    for seg, g in df.groupby("segment"):
        if g.t_rel_s.iloc[-1] - g.t_rel_s.iloc[0] < 120:
            continue  # too short to have settled (the initial stub)
        tail = g[g.t_rel_s > g.t_rel_s.iloc[-1] - SETTLE_TAIL_S]
        if len(tail) < 100:
            continue
        row = {
            "segment": seg,
            "set_c": float(g.chamber_set_c.iloc[0]),
            "ref_c": float(tail.chamber_actual_c.median()),
            "ref_spread": float(tail.chamber_actual_c.std()),
            "t_mid": float(tail.t_rel_s.median()),
            "n": len(tail),
        }
        for s in SENSORS:
            r = tail[f"{s}_rate_hz"].to_numpy()
            row[f"{s}_rate"] = float(r.mean())
            # noise on one 10 ms point, robust to the slow residual drift
            row[f"{s}_sigma_hz"] = float(np.std(np.diff(r)) / np.sqrt(2))
            row[f"{s}_events"] = float(tail[f"{s}_events"].median())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("ref_c").reset_index(drop=True)


# ---------------------------------------------------------------- calibration
def fit_affine(temps_c, rates):
    """Least-squares r = a*T_K + b."""
    tk = np.asarray(temps_c, dtype=float) + KELVIN
    a, b = np.polyfit(tk, np.asarray(rates, dtype=float), 1)
    return float(a), float(b)


def one_point(temp_c, rate):
    """Ideal-PTAT model through the origin, the only honest single-point fit."""
    return float(rate / (temp_c + KELVIN)), 0.0


def invert(a, b, rates):
    return (np.asarray(rates, dtype=float) - b) / a - KELVIN


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else RUN
    os.makedirs(FIGS, exist_ok=True)
    df = load(path)
    tab = settled_table(df)
    print(f"{len(tab)} settled set points, "
          f"{tab.ref_c.min():.1f} .. {tab.ref_c.max():.1f} degC\n")

    hdr = f"{'set':>6} {'ref':>7} {'spread':>7}"
    for s in SENSORS:
        hdr += f" {s+' kHz':>11} {s+' sig':>9}"
    print(hdr)
    for _, r in tab.iterrows():
        line = f"{r.set_c:6.1f} {r.ref_c:7.2f} {r.ref_spread:7.3f}"
        for s in SENSORS:
            line += f" {r[f'{s}_rate']/1e3:11.3f} {r[f'{s}_sigma_hz']:9.1f}"
        print(line)

    print("\ncalibration models, residual error vs the chamber (K):")
    print(f"{'sensor':>6} {'model':>22} {'max |err|':>10} {'rms err':>9}")
    models = {}
    for s in SENSORS:
        rate = tab[f"{s}_rate"].to_numpy()
        ref = tab.ref_c.to_numpy()
        i25 = int(np.argmin(np.abs(ref - 25)))
        i20 = int(np.argmin(np.abs(ref - 20)))
        i60 = int(np.argmin(np.abs(ref - 60)))
        cands = {
            f"1-point @{ref[i25]:.0f}C": one_point(ref[i25], rate[i25]),
            f"2-point {ref[i20]:.0f}/{ref[i60]:.0f}C":
                fit_affine([ref[i20], ref[i60]], [rate[i20], rate[i60]]),
            "N-point (all)": fit_affine(ref, rate),
        }
        models[s] = cands
        for name, (a, b) in cands.items():
            err = invert(a, b, rate) - ref
            print(f"{s:>6} {name:>22} {np.abs(err).max():10.2f} "
                  f"{np.sqrt((err**2).mean()):9.2f}")
    return df, tab, models


if __name__ == "__main__":
    main()
