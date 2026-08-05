#!/usr/bin/env python3
"""Merge the per-event Saleae results into data/deck_data.json.

Two things the demo board cannot do, and one open question.

The board reduces on the fly - one value per 907 periods - so its spectrum
stops at 500 Hz. Keeping every edge turns the same sensor into a 910 kHz record
reaching 455 kHz, which is what shows that GR07's quantisation noise is white
rather than shaped. And the Saleae's 4 ns sample against the board's 15.6 ns
tick gives a second, independent measurement of GR06's noise with a 4x lower
instrument floor.

It also captures GR06 fresh, because that comparison is only worth anything if
both numbers are reproducible rather than remembered.

    python3 scripts/saleae_deck.py              # capture GR06, reuse capture.parquet
    python3 scripts/saleae_deck.py --no-capture # analysis only
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from noise_deck import thin_log  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
DECK = os.path.join(DATA, "deck_data.json")
CAP = os.path.join(DATA, "capture.parquet")
GR06_NPY = os.path.join(DATA, "gr06_saleae_widths.npy")
CLK_HZ = 64e6
SALEAE_TICK_S = 4e-9          # 250 MS/s, one channel
BOARD_TICK_S = 2 / 128e6      # the PIO width loop, 15.6 ns


def capture_gr06(seconds=1.0):
    """One closed-loop GR06 run: the board pulses, the Saleae times the reply."""
    from jnwtemp.acquire import Acquisition, AcquireSettings
    st = AcquireSettings(sensor="GR06", source="saleae",
                         sample_rate=250_000_000, threshold_volts=1.2,
                         duration_s=seconds, bin_ms=seconds * 1000)
    acq = Acquisition(st)
    acq.open()
    try:
        acq.configure_board()
        r = acq.read({})
        rd = r["GR06"] if isinstance(r, dict) else r
        return np.asarray(rd.event_s, dtype=float)
    finally:
        acq.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-capture", action="store_true")
    args = ap.parse_args()

    out = {}

    # ---- GR07: is the quantisation noise white or shaped?
    if os.path.exists(CAP):
        d = pd.read_parquet(CAP)
        per = d["observable_s"].to_numpy()
        fs = 1.0 / per.mean()
        x = per / per.mean() - 1.0
        f, p = signal.welch(x, fs=fs, nperseg=16384, detrend="constant")
        q = (per.mean() * CLK_HZ) % 1
        band = f > 1
        pf, pp = thin_log(f[1:], p[1:])
        out["gr07"] = {
            "periods": int(per.size),
            "fs_hz": float(fs),
            "nyquist_hz": float(fs / 2),
            "board_nyquist_hz": 500.0,
            "sigma_ppm": float(x.std(ddof=1) * 1e6),
            "integral_ppm": float(np.sqrt(np.trapezoid(p, f)) * 1e6),
            "dither_pred_ppm": float(np.sqrt(q * (1 - q))
                                     * ((1 / CLK_HZ) / per.mean()) * 1e6),
            "flat_variation": float(p[band].max() / p[band].min()),
            "psd": [[round(a, 3), round(b * 1e6, 3)] for a, b in zip(pf, pp)],
            # first-order shaping, normalised at the low end, for the overlay
            "shaped": [[round(a, 3),
                        round(float(np.sqrt(4 * np.sin(np.pi * a / fs) ** 2))
                              * pp[0] / float(np.sqrt(4 * np.sin(np.pi * pf[0] / fs) ** 2))
                              * 1e6, 3)]
                       for a in pf],
        }
        print(f"  GR07: {per.size:,} periods to {fs/2/1e3:.0f} kHz, "
              f"flat within {out['gr07']['flat_variation']:.0f}x, "
              f"integrates to {out['gr07']['integral_ppm']:.0f} ppm")

    # ---- GR06: the same noise, measured with a 4x finer timebase
    w = None
    if not args.no_capture:
        try:
            w = capture_gr06()
            np.save(GR06_NPY, w)
        except Exception as exc:
            print(f"  GR06 capture failed ({exc}); falling back to the saved run")
    if w is None and os.path.exists(GR06_NPY):
        w = np.load(GR06_NPY)
    if w is not None and w.size > 100:
        # Compare the WHITE FLOOR, not the total sigma. The board's number is
        # a 32 min integral that includes 1/f and the RTS trap; a 1 s Saleae
        # capture cannot contain the same low-frequency content, so the two
        # totals are not measuring the same quantity and agreed to 0.2% once by
        # luck. The white floor is a spectral density and is bandwidth-free.
        fs6 = 1.0 / (w.mean() + 220e-6)
        xw = w / w.mean() - 1.0
        f6, p6 = signal.welch(xw, fs=fs6, nperseg=min(1024, xw.size // 4),
                              detrend="constant")
        hi6 = f6 > fs6 / 4
        sal_white = float(np.sqrt(np.median(p6[hi6])) * 1e6)
        sal_wfloor = float(SALEAE_TICK_S / np.sqrt(12) / w.mean()
                           * np.sqrt(2 / fs6) * 1e6)
        out["gr06_white"] = {
            "saleae_ppm_rthz": sal_white,
            "saleae_floor_ppm_rthz": sal_wfloor,
            "board_ppm_rthz": 43.7,          # from the 32 min board run
            "board_floor_ppm_rthz": 13.7,
            "agree_pct": float(abs(sal_white - 43.7) / 43.7 * 100),
        }
        print(f"  GR06 white floor: {sal_white:.1f} ppm/rtHz (Saleae, floor "
              f"{sal_wfloor:.1f}) vs 43.7 (board, floor 13.7) - "
              f"{out['gr06_white']['agree_pct']:.0f}% apart")
        sal_total = float(w.std(ddof=1) / w.mean() * 1e6)
        sal_floor = float(SALEAE_TICK_S / np.sqrt(12) / w.mean() * 1e6)
        # the board's 32 min run, for the same quantity
        board_total, board_floor = 2277.0, 642.0
        sensor_sal = float(np.sqrt(max(sal_total**2 - sal_floor**2, 0)))
        sensor_brd = float(np.sqrt(max(board_total**2 - board_floor**2, 0)))
        out["gr06"] = {
            "pulses": int(w.size),
            "width_ns": float(w.mean() * 1e9),
            "saleae": {"tick_ns": SALEAE_TICK_S * 1e9, "total_ppm": sal_total,
                       "floor_ppm": sal_floor, "sensor_ppm": sensor_sal},
            "board": {"tick_ns": BOARD_TICK_S * 1e9, "total_ppm": board_total,
                      "floor_ppm": board_floor, "sensor_ppm": sensor_brd},
            "disagreement_pct": float(abs(sensor_sal - sensor_brd)
                                      / sensor_brd * 100),
            "floor_ratio": float(board_floor / sal_floor),
        }
        print(f"  GR06: {w.size:,} pulses; sensor alone {sensor_sal:.0f} ppm "
              f"(Saleae) vs {sensor_brd:.0f} ppm (board), "
              f"{out['gr06']['disagreement_pct']:.1f}% apart")

    # ---- the open question, recorded as measurements rather than a conclusion
    out["tone"] = {
        "cold": {"fs_khz": 911.41, "at_125_khz": 277.0, "at_250_khz": 358.2},
        "warm": {"fs_khz": 915.40, "at_125_khz": 265.8, "at_250_khz": 384.0},
        "level_swing": {"low": 12.6, "high": 3686.0, "fs_change_khz": 1.0},
        "status": ("part tracks the Saleae's sample rate and is instrumental; "
                   "part appears at both rates and is in the signal. Its "
                   "amplitude swings ~300x for a 0.1% change in fs, which "
                   "points at quantiser idle tones rather than the supply. "
                   "Not settled."),
    }

    deck = json.load(open(DECK)) if os.path.exists(DECK) else {}
    deck["saleae"] = out
    json.dump(deck, open(DECK, "w"), indent=1)
    print("merged into", os.path.relpath(DECK, HERE))


if __name__ == "__main__":
    main()
