#!/usr/bin/env python3
"""Merge the chamber characterisation into data/deck_data.json.

Reads only the committed extract, never the 82 MB raw run, so the deck can be
rebuilt from a fresh clone. Run after analyse_dual.py (which creates the file)
and before build_presentation.py.

    python3 scripts/analyse_dual.py
    python3 scripts/chamber_deck.py
    python3 scripts/build_presentation.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from chamber_analyse import CLK_HZ, DATA, KELVIN, SENSORS, fit_affine  # noqa: E402
from chamber_burst import K_EV, MIN_SNR, two_state  # noqa: E402
from chamber_figures import EXTRACT, calibration_models  # noqa: E402

#: Dwell shown as the telegraph trace. 25 degC is where the two levels are both
#: well separated and slow enough to read as flat steps rather than spikes.
BURST_DWELL = 5
BURST_WINDOW_S = 60.0

DECK = os.path.join(DATA, "deck_data.json")


def quantisation_section(tab):
    """How much of GR07's clock quantisation survives into a dwell mean.

    The answer is essentially none, which is the opposite of what the first
    pass at this concluded. GR07's period is quantised to whole 64 MHz cycles,
    but it sits at some fraction p between two codes and alternates between
    them, so the quantiser is *dithered* and its mean is recovered by
    averaging. A dithered quantiser at fraction p has per-period sigma
    sqrt(p(1-p)) * LSB; over the events in a bin that predicts the measured
    per-bin noise almost exactly, which is the evidence that it really is
    dither and not something else.
    """
    a, _ = fit_affine(tab.ref_c, tab.GR07_rate_hz)
    cyc = (1e9 / tab.GR07_rate_hz) / (1e9 / CLK_HZ)
    p = cyc % 1
    lsb_hz = tab.GR07_rate_hz.mean() ** 2 / CLK_HZ
    pred = np.sqrt(p * (1 - p)) * lsb_hz / np.sqrt(tab.GR07_events_per_bin)
    sem_mk = tab.GR07_sigma_hz / np.sqrt(tab.n_samples) / abs(a) * 1000
    return {
        "lsb_k": round(float(lsb_hz / abs(a)), 2),
        "clk_mhz": CLK_HZ / 1e6,
        "dither_model_r": round(float(np.corrcoef(pred, tab.GR07_sigma_hz)[0, 1]), 3),
        "sigma_bin_mk": [round(float(v) / abs(a) * 1000, 1) for v in tab.GR07_sigma_hz],
        "in_mean_mk_lo": round(float(sem_mk.min()), 2),
        "in_mean_mk_hi": round(float(sem_mk.max()), 2),
    }


def burst_section(slope):
    """Random-telegraph noise in GR06, reduced to what the slide draws."""
    d = pd.read_csv(f"{EXTRACT}_dwell.csv")
    per = []
    for k, g in d.groupby("dwell"):
        v = two_state(g.t_rel_s.to_numpy(), g.GR06_rate_hz.to_numpy())
        per.append({"dwell": int(k), "ref_c": round(float(g.chamber_actual_c.median()), 1),
                    "step_mk": round(v["step"] / slope * 1000, 1),
                    "life_ms": None if np.isnan(v["dur"]) else round(v["dur"] * 1000, 1),
                    "frac": round(v["frac"], 3),
                    "resolved": bool(v["snr"] > MIN_SNR and not np.isnan(v["dur"]))})

    ok = [p for p in per if p["resolved"]]
    x = np.array([1.0 / (K_EV * (p["ref_c"] + KELVIN)) for p in ok])
    y = np.log(np.array([1.0 / (p["life_ms"] / 1000) for p in ok]))
    sl, ic = np.polyfit(x, y, 1)

    # the trace itself, as the 0.3 s median - that is what makes the two levels
    # visible, and it thins 6000 samples to something a slide can draw
    g = d[d.dwell == BURST_DWELL]
    t = g.t_rel_s.to_numpy()
    v = two_state(t, g.GR06_rate_hz.to_numpy())
    t = t - t[0]
    m = t <= BURST_WINDOW_S
    step = max(1, int(m.sum() // 600))
    trace = [[round(float(a), 2), round(float(b) / slope * 1000, 1)]
             for a, b in zip(t[m][::step], v["median"][m][::step])]
    hist, edges = np.histogram(v["detrended"][m] / slope * 1000, bins=70)

    return {
        "dwell": BURST_DWELL,
        "ref_c": round(float(g.chamber_actual_c.median()), 1),
        "trace_mk": trace,
        "hist": [[round(float(e), 1), int(c)] for e, c in zip(edges[:-1], hist)],
        "per_dwell": per,
        "mean_step_mk": round(float(np.mean([p["step_mk"] for p in per])), 0),
        "ea_mev": round(float(-sl * 1000), 0),
        "r": round(float(np.corrcoef(x, y)[0, 1]), 3),
        "life_slow_ms": round(max(p["life_ms"] for p in ok), 0),
        "life_fast_ms": round(min(p["life_ms"] for p in ok), 0),
        "frac_lo": round(min(p["frac"] for p in ok), 3),
        "frac_hi": round(max(p["frac"] for p in ok), 3),
    }


def main() -> None:
    tab = pd.read_csv(f"{EXTRACT}_summary.csv")
    dnl = pd.read_csv(f"{EXTRACT}_dnl.csv")
    meta = json.load(open(f"{EXTRACT}.meta.json"))
    ref = tab.ref_c.to_numpy()

    out = {
        "ref_c": [round(float(v), 2) for v in ref],
        "set_c": [round(float(v), 1) for v in tab.set_c],
        "tail_s": meta["derived"]["dwell_tail_s"],
        "n_points": len(tab),
        "dnl": {
            "lsb_k": round(float(dnl.lsb_k.iloc[0]), 2),
            "peak_lsb": round(float(dnl.dnl_lsb.abs().max()), 3),
            "codes": int(len(dnl)),
        },
        "sensors": {},
    }

    for s in SENSORS:
        rate = tab[f"{s}_rate_hz"].to_numpy()
        a, b = fit_affine(ref, rate)
        # INL: residual after the best straight line, expressed in kelvin so it
        # is comparable between two sensors whose rates differ 6x.
        inl = (rate - (a * (ref + KELVIN) + b)) / a
        models = calibration_models(ref, rate)
        out["sensors"][s] = {
            "slope_hz_per_k": round(a, 2),
            "rate_khz": [round(float(v) / 1e3, 3) for v in rate],
            "inl_k": [round(float(v), 4) for v in inl],
            "inl_peak_k": round(float(np.abs(inl).max()), 3),
            "noise_mk": [round(float(v) / abs(a) * 1000, 1)
                         for v in tab[f"{s}_sigma_hz"]],
            "cal": [{"name": n,
                     "err_k": [round(float(v), 4) for v in e],
                     "max_k": round(float(np.abs(e).max()), 3)}
                    for n, e in models.items()],
        }

    # Split the INL into what both sensors share and what is each sensor's own.
    # Two different architectures - one clocked, one asynchronous - do not share
    # a nonlinearity; what they share is the temperature they were actually at
    # versus what the chamber reported, so the common part is reference error
    # (probe plus the die-to-air gap), not silicon.
    inls = {s_: np.array(out["sensors"][s_]["inl_k"]) for s_ in SENSORS}
    common = sum(inls.values()) / len(inls)
    out["inl_common_k"] = [round(float(v), 4) for v in common]
    out["inl_common_peak_k"] = round(float(np.abs(common).max()), 3)
    for s_ in SENSORS:
        own = inls[s_] - common
        out["sensors"][s_]["inl_own_k"] = [round(float(v), 4) for v in own]
        out["sensors"][s_]["inl_own_peak_k"] = round(float(np.abs(own).max()), 3)

    out["quantisation"] = quantisation_section(tab)
    out["burst"] = burst_section(slope=out["sensors"]["GR06"]["slope_hz_per_k"])

    deck = json.load(open(DECK)) if os.path.exists(DECK) else {}
    deck["chamber"] = out
    json.dump(deck, open(DECK, "w"), indent=1)

    print(f"{out['n_points']} set points, {ref.min():.1f} .. {ref.max():.1f} degC")
    for s in SENSORS:
        v = out["sensors"][s]
        best = min(v["cal"], key=lambda c: c["max_k"])
        print(f"  {s}: {v['slope_hz_per_k']/1e3:6.2f} kHz/K   "
              f"INL peak {v['inl_peak_k']:.2f} K   "
              f"best cal {best['name']} -> {best['max_k']:.2f} K")
    b = out["burst"]
    print(f"  RTS: step {b['mean_step_mk']:.0f} mK, Ea {b['ea_mev']:.0f} meV "
          f"(r={b['r']:+.2f}), lifetime {b['life_slow_ms']:.0f} -> "
          f"{b['life_fast_ms']:.0f} ms")
    print("merged into", os.path.relpath(DECK, HERE))


if __name__ == "__main__":
    main()
