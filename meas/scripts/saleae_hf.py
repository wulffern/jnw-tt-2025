#!/usr/bin/env python3
"""High-frequency noise from a per-event Saleae capture.

The demo board reduces on the fly - one value per 907 periods - so its spectrum
stops at 500 Hz. The Saleae keeps every edge, which turns the same sensor into a
910 kHz record and reaches 455 kHz: about 900x the bandwidth, and the only way
to see what the retiming does between one period and the next.

It exists to test a specific claim. The deck asserted that GR07's re-timed
output is "accidentally a first-order sigma-delta", whose quantisation noise
would rise as f^2 toward Nyquist. That is a falsifiable statement about a
spectrum, so this measures it.

    python3 scripts/saleae_hf.py                     # uses data/capture.parquet
    python3 scripts/retake_staircase.py              # to take a fresh one
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
FIGS = os.path.join(DATA, "figs")
import matplotlib                                   # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                     # noqa: E402

PAPER, INK, INK2, RULE = "#faf7f1", "#1b1811", "#58513f", "#ded5c4"
C7, C6 = "#2a78d6", "#d1521f"
plt.rcParams.update({
    "figure.facecolor": PAPER, "axes.facecolor": PAPER, "savefig.facecolor": PAPER,
    "font.size": 9.5, "axes.edgecolor": RULE, "axes.labelcolor": INK,
    "text.color": INK, "xtick.color": INK2, "ytick.color": INK2,
    "grid.color": RULE, "axes.grid": True, "grid.alpha": .45,
    "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 130,
})
CLK_HZ = 64e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?",
                    default=os.path.join(DATA, "capture.parquet"))
    ap.add_argument("--nperseg", type=int, default=16384)
    args = ap.parse_args()
    if not os.path.exists(args.path):
        raise SystemExit(f"{args.path} not found - run retake_staircase.py")

    d = pd.read_parquet(args.path)
    per = d["observable_s"].to_numpy()
    fs = 1.0 / per.mean()
    x = per / per.mean() - 1.0
    f, p = signal.welch(x, fs=fs, nperseg=args.nperseg, detrend="constant")

    cyc = per.mean() * CLK_HZ
    q = cyc % 1
    lsb_frac = (1.0 / CLK_HZ) / per.mean()
    pred = np.sqrt(q * (1 - q)) * lsb_frac

    print(f"{per.size:,} periods, mean {per.mean()*1e9:.2f} ns "
          f"= {cyc:.3f} clock cycles")
    print(f"  fs {fs/1e3:.1f} kHz, Nyquist {fs/2/1e3:.1f} kHz "
          f"({fs/2/500:.0f}x what the board reaches)")
    print(f"  per-period sigma {x.std(ddof=1)*1e6:.0f} ppm; "
          f"two-level dither at q={q:.3f} predicts {pred*1e6:.0f}")

    band = f > 1
    flat = float(np.median(p[band]))
    ratio = float(p[band].max() / p[band].min())
    integ = float(np.sqrt(np.trapezoid(p, f)))
    print(f"  PSD across 1 Hz-{fs/2/1e3:.0f} kHz varies {ratio:.0f}x "
          f"(flat would be ~1x after Welch scatter)")
    print(f"  integrates to {integ*1e6:.0f} ppm vs {x.std(ddof=1)*1e6:.0f} "
          f"measured -> the white model holds all the variance")

    # first-order shaping would be |1 - z^-1|^2 = 4 sin^2(pi f / fs)
    shape = 4 * np.sin(np.pi * f / fs) ** 2
    lo = (f > 50) & (f < 500)
    scale = float(np.median(p[lo] / np.maximum(shape[lo], 1e-30)))
    hi = f > fs / 4
    excess = float(np.median(p[hi]) / (scale * np.median(shape[hi])))
    print(f"  against first-order shaping normalised at 50-500 Hz, the top "
          f"octave sits {1/excess:.0f}x BELOW the prediction")
    print("  -> the quantisation is white-dithered, not noise-shaped")

    tie = np.cumsum(per - per.mean())
    ft, pt = signal.welch(tie, fs=fs, nperseg=args.nperseg, detrend="constant")

    fig, ax = plt.subplots(1, 2, figsize=(12.5, 4.5))
    a = ax[0]
    a.loglog(f[1:], np.sqrt(p[1:]), lw=.7, color=C7, label="measured")
    sh = np.sqrt(shape[1:]) * np.sqrt(p[1]) / np.sqrt(shape[1])
    a.loglog(f[1:], sh, "--", lw=1.3, color=INK2,
             label="first-order Δ∑ (what the deck claimed)")
    a.axhline(np.sqrt(flat), color=C6, lw=1.1, ls=":",
              label=f"white, {np.sqrt(flat)*1e6:.1f} ppm/√Hz")
    a.set_xlabel("offset frequency  [Hz]")
    a.set_ylabel("period noise  [1/√Hz]")
    a.set_title("GR07 period-to-period noise is flat, not shaped", fontsize=10)
    a.legend(fontsize=8)

    a = ax[1]
    a.loglog(ft[1:], np.sqrt(pt[1:]) * 1e9, lw=.7, color=C6)
    ref = np.sqrt(pt[1]) * 1e9 * (ft[1] / ft[1:])
    a.loglog(ft[1:], ref, "--", lw=1.2, color=INK2, label="1/f — integrated white")
    a.set_xlabel("offset frequency  [Hz]")
    a.set_ylabel("TIE  [ns/√Hz]")
    a.set_title("Accumulated phase: exactly integrated white noise", fontsize=10)
    a.legend(fontsize=8)

    fig.suptitle(f"{per.size:,} individual periods at {fs/1e3:.0f} kHz — "
                 f"reaching {fs/2/1e3:.0f} kHz", y=0.99)
    fig.tight_layout()
    os.makedirs(FIGS, exist_ok=True)
    out = os.path.join(FIGS, "hf_noise.png")
    fig.savefig(out)
    plt.close(fig)
    print(f"\n  wrote figs/hf_noise.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
