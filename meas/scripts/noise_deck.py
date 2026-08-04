#!/usr/bin/env python3
"""Merge the long noise runs and the correlation test into data/deck_data.json.

The raw captures are tens of megabytes and stay local, so what goes into the
deck is the reduced spectrum: a decimated PSD per sensor, the Allan deviation,
the instrument floors each has to beat, and the correlation result. That is a
few kilobytes and lets every number on the slides be regenerated rather than
typed in.

    python3 scripts/noise_deck.py                    # newest run per sensor
    python3 scripts/noise_deck.py --gr07 <run.u32>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from jnwtemp.spectrum import allan_deviation  # noqa: E402
from noise_analyse import (  # noqa: E402
    DESIGN_C_F, DESIGN_VREF_V, chunked_psd, load, low_band_psd,
)

DATA = os.path.join(os.path.dirname(HERE), "data")
DECK = os.path.join(DATA, "deck_data.json")
K_B, T_K = 1.380649e-23, 300.0
#: Points kept per decade when thinning a spectrum for the slide. A PSD drawn
#: at full resolution is 8000 points of hair; on a log axis a few dozen per
#: decade is all that survives rasterisation anyway.
PER_DECADE = 24


def thin_log(f, p, per_decade=PER_DECADE):
    """Geometric-mean bins on a log frequency axis."""
    ok = (f > 0) & np.isfinite(p) & (p > 0)
    f, p = f[ok], p[ok]
    if f.size < 4:
        return [], []
    lo, hi = np.log10(f[0]), np.log10(f[-1])
    n = max(4, int((hi - lo) * per_decade))
    edges = np.logspace(lo, hi, n + 1)
    idx = np.digitize(f, edges) - 1
    out_f, out_p = [], []
    for k in range(n):
        m = idx == k
        if not m.any():
            continue
        out_f.append(float(np.exp(np.mean(np.log(f[m])))))
        out_p.append(float(np.sqrt(np.median(p[m]))))     # amplitude, ppm/rtHz
    return out_f, out_p


def sensor_section(path):
    meta, value, fs, unit, label, bounds = load(path)
    s = meta["sensor"]
    nper = min(16384, min(b - a for a, b in bounds))
    f, p = chunked_psd(value, fs, bounds, nper)
    fl, pl = low_band_psd(value, bounds, meta["chunks"])

    hi = f > fs / 4
    white = float(np.sqrt(np.median(p[hi]))) if hi.any() else float("nan")

    tick = meta["tick_s"]
    q = tick / np.sqrt(12)
    mean = float(value.mean())
    if s == "GR07":
        per = meta["periods_per_sample"]
        floor = q / (per / mean)
        clk = 1.0 / meta["project_clock_hz"]
        floors = {"timebase": floor,
                  "retiming": clk / np.sqrt(12) / np.sqrt(per) * mean}
    else:
        floors = {"timebase": q / mean}

    taus, dev = allan_deviation(value / mean, 1.0 / fs)
    good = np.isfinite(dev) & (dev > 0)
    step = max(1, int(good.sum() // 40))

    hf, hp = thin_log(f[1:], p[1:])
    lf, lp = (thin_log(fl, pl) if fl is not None else ([], []))
    return {
        "sensor": s,
        "samples": int(value.size),
        "minutes": float(sum(c["elapsed_us"] for c in meta["chunks"]) / 6e7),
        "duty": float(meta["duty_cycle"]),
        "fs_hz": float(fs),
        "mean": mean,
        "unit": "s" if s == "GR06" else "Hz",
        "sigma_ppm": float(value.std(ddof=1) / mean * 1e6),
        "white_ppm_rthz": round(white * 1e6, 2),
        "floors_ppm_rthz": {k: round(v * np.sqrt(2 / fs) * 1e6, 2)
                            for k, v in floors.items()},
        "psd": [[round(a, 5), round(b * 1e6, 3)] for a, b in zip(hf, hp)],
        "psd_low": [[round(a, 6), round(b * 1e6, 3)] for a, b in zip(lf, lp)],
        "allan": [[round(float(t), 5), round(float(d) * 1e6, 3)]
                  for t, d in zip(np.asarray(taus)[good][::step],
                                  np.asarray(dev)[good][::step])],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gr06", default=None)
    ap.add_argument("--gr07", default=None)
    ap.add_argument("--corr", default=None)
    args = ap.parse_args()

    out = {}
    for key, given in (("GR06", args.gr06), ("GR07", args.gr07)):
        # A run in progress has no sidecar yet - it is written when the run
        # ends - so pick the newest *complete* one rather than the newest file.
        done = [f for f in sorted(glob.glob(os.path.join(DATA, f"noise-{key}-*.u32")))
                if os.path.exists(os.path.splitext(f)[0] + ".meta.json")]
        path = given or (done[-1] if done else None)
        if not path or not os.path.exists(path):
            print(f"  no completed {key} run found - skipping")
            continue
        sec = sensor_section(path)
        out[key] = sec
        print(f"  {key}: {sec['samples']:,} samples, {sec['minutes']:.1f} min, "
              f"white {sec['white_ppm_rthz']:.1f} ppm/rtHz, floors "
              f"{sec['floors_ppm_rthz']}")

    if "GR06" in out:
        # The design is known, so kT/C is a prediction to check, not a fit.
        kt_c = np.sqrt(K_B * T_K / DESIGN_C_F)
        pred = kt_c / DESIGN_VREF_V
        w = out["GR06"]["white_ppm_rthz"] * 1e-6 * np.sqrt(out["GR06"]["fs_hz"] / 2)
        out["ktc"] = {
            "cap_f": DESIGN_C_F, "vref_v": DESIGN_VREF_V,
            "sqrt_ktc_uv": round(kt_c * 1e6, 1),
            "pred_ppm": round(pred * 1e6, 0),
            "white_ppm": round(w * 1e6, 0),
            "share_pct": round((pred / w) ** 2 * 100, 1),
        }
        print(f"  kT/C {out['ktc']['pred_ppm']:.0f} ppm = "
              f"{out['ktc']['share_pct']:.1f}% of the white power")

    corr = args.corr or (sorted(glob.glob(
        os.path.join(DATA, "corr-dual-*.meta.json")) or [None])[-1])
    if corr and os.path.exists(corr):
        import corr_analyse as ca
        meta, f7, w6, ix = ca.load(corr)
        v7, v6, bounds = ca.align(f7, w6, ix, meta["chunks"])
        fs = 1.0 / (meta["gr07_periods_per_sample"] / f7.mean())
        f, pxx, pyy, pxy, nseg = ca.spectra(v6, v7, fs, bounds, 1024)
        coh = np.abs(pxy) ** 2 / (pxx * pyy)
        band = (2.0 / ca.DIE_TAU_S, fs / 2 * 0.8)
        sel = (f >= band[0]) & (f < band[1])
        lim = max(float(np.median(coh[sel])), 1.0 / nseg)
        shared = float(np.sqrt(lim * np.median(pxx[sel]))) * 1e6
        total = float(np.sqrt(np.median(pxx[sel]))) * 1e6
        out["corr"] = {
            "pairs": int(v7.size), "minutes": float(v7.size / fs / 60),
            "nseg": int(nseg), "bias": round(1.0 / nseg, 5),
            "coherence": round(float(np.median(coh[sel])), 5),
            "band_hz": [round(band[0], 2), round(band[1], 0)],
            "shared_ppm_rthz": round(shared, 2),
            "total_ppm_rthz": round(total, 1),
            "shared_pct": round(shared / total * 100, 1),
            "supply_uv_rthz": round(shared * ca.VDD_V, 2),
            "supply_needed_uv_rthz": 79.0,
        }
        print(f"  corr: coherence {out['corr']['coherence']} vs bias "
              f"{out['corr']['bias']}, shared < {shared:.1f} ppm/rtHz")

    deck = json.load(open(DECK)) if os.path.exists(DECK) else {}
    deck["noise"] = out
    json.dump(deck, open(DECK, "w"), indent=1)
    print("merged into", os.path.relpath(DECK, HERE))


if __name__ == "__main__":
    main()
