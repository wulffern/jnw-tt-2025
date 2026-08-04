#!/usr/bin/env python3
"""Split the two sensors' noise into what they share and what is their own.

Coherence alone does not identify a mechanism - two signals can be coherent for
many reasons. What identifies the supply is the *gain*. Both sensors reference
VDD, so a fractional supply fluctuation appears as

    GR06 width      dt/t  = +dVDD/VDD
    GR07 frequency  df/f  = -dVDD/VDD

i.e. coherent, anti-phase, and unity gain in fractional terms. This reports the
measured gain next to that prediction; coherence says "shared", the gain says
"shared *by the supply*".

Two controls are computed alongside, because a coherence estimate is biased
upward by about 1/N with N averages and will always show something:

  * time-shift null - the same calculation with GR07 displaced by many seconds.
    Real shared noise disappears, estimator bias does not.
  * if an --interleaved run is given, the same again on data where the sensors
    were never measured at the same time, so nothing common can exist.

Usage:
    python3 scripts/corr_analyse.py data/corr-dual-*.meta.json
    python3 scripts/corr_analyse.py data/corr-dual-*.meta.json \\
        --interleaved data/corr-interleaved-*.meta.json
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
import matplotlib                                  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                    # noqa: E402

FIGS = os.path.join(os.path.dirname(HERE), "data", "figs")
PAPER, INK, INK2, RULE = "#faf7f1", "#1b1811", "#58513f", "#ded5c4"
C7, C6, C_COM = "#2a78d6", "#d1521f", "#1baf7a"
plt.rcParams.update({
    "figure.facecolor": PAPER, "axes.facecolor": PAPER, "savefig.facecolor": PAPER,
    "font.size": 9.5, "axes.edgecolor": RULE, "axes.labelcolor": INK,
    "text.color": INK, "xtick.color": INK2, "ytick.color": INK2,
    "grid.color": RULE, "axes.grid": True, "grid.alpha": .45,
    "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 130,
})

#: Die thermal time constant, measured from the chamber step responses. Above
#: this the die cannot follow an air-temperature change, so common noise there
#: is electrical rather than thermal.
DIE_TAU_S = 1.2

#: Nominal ASIC supply, for turning a fractional shared-noise limit into volts.
VDD_V = 1.8


def load(meta_path):
    meta = json.load(open(meta_path))
    stem = meta_path[: -len(".meta.json")]
    t7 = np.fromfile(stem + ".gr07.u32", dtype=np.uint32).astype(float)
    t6 = np.fromfile(stem + ".gr06.u32", dtype=np.uint32).astype(float)
    ix = np.fromfile(stem + ".index.u32", dtype=np.uint32).astype(np.int64)
    per = meta["gr07_periods_per_sample"]
    f7 = per / (t7 * meta["gr07_tick_s"])            # Hz
    w6 = t6 * meta["gr06_tick_s"]                    # s
    return meta, f7, w6, ix


def align(f7, w6, ix, chunks):
    """Put GR06 on GR07's grid, chunk by chunk, using the recorded pulse index.

    Every GR07 sample carries the GR06 pulse count at the instant it was read,
    so each GR07 sample maps to the GR06 pulses that happened during it. No
    interpolation and no assumption that the two rates are exactly commensurate.
    """
    out7, out6, bounds = [], [], []
    a7 = a6 = 0
    for c in chunks:
        n7, n6 = c["n7"], c["n6"]
        s7 = f7[a7:a7 + n7]
        s6 = w6[a6:a6 + n6]
        si = ix[a7:a7 + n7]
        if s7.size != n7 or s6.size != n6:
            break
        # mean GR06 width over the pulses that fall inside each GR07 sample
        lo = np.concatenate(([0], si[:-1]))
        hi = si
        keep = (hi > lo) & (hi <= s6.size)
        if keep.sum() < 32:
            a7 += n7; a6 += n6
            continue
        csum = np.concatenate(([0.0], np.cumsum(s6)))
        m6 = (csum[hi[keep]] - csum[lo[keep]]) / (hi[keep] - lo[keep])
        m7 = s7[keep]
        start = sum(len(x) for x in out7)
        out7.append(m7)
        out6.append(m6)
        bounds.append((start, start + m7.size))
        a7 += n7
        a6 += n6
    if not out7:
        raise SystemExit("nothing aligned - were both sensors actually running?")
    return np.concatenate(out7), np.concatenate(out6), bounds


def spectra(x, y, fs, bounds, nperseg):
    """Auto- and cross-spectra, averaged over chunks. Never across a seam.

    nperseg is fixed rather than shrunk to fit each chunk: a per-chunk length
    gives per-chunk frequency grids, which cannot be averaged together. Chunks
    shorter than the window are skipped and counted, so a run that quietly
    dropped most of its data cannot masquerade as a clean spectrum.

    All state is local. An earlier version accumulated the segment count on the
    function object, which leaked between the measurement and its own control
    and made the control look better than it was.
    """
    acc = None
    total = 0
    nseg = 0
    skipped = 0
    freqs = None
    for a, b in bounds:
        if b - a < nperseg:
            skipped += 1
            continue
        xs = x[a:b] / x[a:b].mean() - 1.0
        ys = y[a:b] / y[a:b].mean() - 1.0
        f, pxx = signal.welch(xs, fs=fs, nperseg=nperseg, detrend="constant")
        _, pyy = signal.welch(ys, fs=fs, nperseg=nperseg, detrend="constant")
        _, pxy = signal.csd(xs, ys, fs=fs, nperseg=nperseg, detrend="constant")
        w = b - a
        cur = np.array([pxx, pyy, pxy.real, pxy.imag]) * w
        acc = cur if acc is None else acc + cur
        total += w
        nseg += max(1, 2 * (b - a) // nperseg - 1)   # Welch's 50% overlap
        freqs = f
    if acc is None:
        raise SystemExit(f"every chunk was shorter than nperseg={nperseg}; "
                         f"use --nperseg below {min(b-a for a, b in bounds)}")
    if skipped:
        print(f"  ({skipped} of {len(bounds)} chunks too short for "
              f"nperseg={nperseg}, skipped)")
    acc /= total
    return freqs, acc[0], acc[1], acc[2] + 1j * acc[3], nseg


def report(tag, f, pxx, pyy, pxy, nseg, band):
    """Coherence, and a *bracket* on the shared-term gain.

    With independent noise on both channels the gain is not identifiable from
    a single ratio: |Pxy|/Pxx is attenuated by GR06's own noise and Pyy/|Pxy|
    is inflated by GR07's, so the truth is between them. Quoting either alone
    would have reported the supply gain as 0.87 when the fabricated truth was
    exactly 1. The interval is the honest answer; if it straddles unity the
    measurement is consistent with the supply.
    """
    coh = np.abs(pxy) ** 2 / (pxx * pyy)
    sel = (f >= band[0]) & (f < band[1])
    if not sel.any():
        return None
    lo = np.abs(pxy[sel]) / pxx[sel]
    hi = pyy[sel] / np.maximum(np.abs(pxy[sel]), 1e-300)
    phase = np.angle(np.median(pxy[sel].real) + 1j * np.median(pxy[sel].imag),
                     deg=True)
    return {
        "tag": tag, "coh": float(np.median(coh[sel])),
        "gain_lo": float(np.median(lo)), "gain_hi": float(np.median(hi)),
        "phase_deg": float(phase),
        "bias": 1.0 / max(nseg, 1), "nseg": nseg,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("meta", nargs="?", default=None)
    ap.add_argument("--interleaved", default=None)
    ap.add_argument("--nperseg", type=int, default=1024)
    args = ap.parse_args()
    path = args.meta or (sorted(glob.glob(os.path.join(
        os.path.dirname(HERE), "data", "corr-dual-*.meta.json")) or [None])[-1])
    if not path:
        raise SystemExit("no corr-dual-*.meta.json found; run corr_run.py first")

    meta, f7, w6, ix = load(path)
    v7, v6, bounds = align(f7, w6, ix, meta["chunks"])
    dt = meta["gr07_periods_per_sample"] / f7.mean()
    fs = 1.0 / dt
    print(f"{os.path.basename(path)}")
    print(f"  {v7.size:,} aligned pairs at {fs:.1f} Hz "
          f"({v7.size/fs/60:.1f} min), {len(bounds)} chunks")
    print(f"  GR07 {v7.mean()/1e3:.3f} kHz, GR06 {v6.mean()*1e9:.1f} ns")

    f, pxx, pyy, pxy, nseg = spectra(v6, v7, fs, bounds, args.nperseg)

    # electrical band: above the die's thermal cut-off, so not temperature
    band = (max(2.0 / DIE_TAU_S, f[1]), fs / 2 * 0.8)
    rows = [report("measured", f, pxx, pyy, pxy, nseg, band)]

    # control 1: shift GR07 by many seconds - real common-mode cannot survive
    # Shift GR07 by whole chunks: rolling within a chunk would still overlap
    # the same supply excursion, which is exactly what the control must avoid.
    if len(bounds) > 3:
        roll = max(1, len(bounds) // 3)
        shifted = [bounds[(i + roll) % len(bounds)] for i in range(len(bounds))]
        n = min(min(b - a for a, b in bounds), min(b - a for a, b in shifted))
        pairs6 = [v6[a:a + n] for a, b in bounds]
        pairs7 = [v7[a:a + n] for a, b in shifted]
        cat6 = np.concatenate(pairs6)
        cat7 = np.concatenate(pairs7)
        nb = [(i * n, (i + 1) * n) for i in range(len(pairs6))]
        f2, qxx, qyy, qxy, n2 = spectra(cat6, cat7, fs, nb, args.nperseg)
        rows.append(report("time-shift null", f2, qxx, qyy, qxy, n2, band))

    if args.interleaved and os.path.exists(args.interleaved):
        m2, g7, g6, gi = load(args.interleaved)
        u7, u6, ib = align(g7, g6, gi, m2["chunks"])
        f3, rxx, ryy, rxy, n3 = spectra(u6, u7, fs, ib, args.nperseg)
        rows.append(report("interleaved control", f3, rxx, ryy, rxy, n3, band))

    print(f"\n  band {band[0]:.2f}-{band[1]:.0f} Hz "
          f"(above the die's {DIE_TAU_S:g} s thermal cut-off, so not temperature)")
    print(f"  {'':22} {'coherence':>10} {'bias 1/N':>9} {'|gain| range':>16} "
          f"{'phase':>8}")
    for r in rows:
        if r is None:
            continue
        print(f"  {r['tag']:22} {r['coh']:10.4f} {r['bias']:9.4f} "
              f"{r['gain_lo']:7.3f}-{r['gain_hi']:<8.3f} {r['phase_deg']:+7.0f}°")
    print("\n  supply noise predicts |gain| straddling 1.00 at a phase of 180°")
    print("  (GR06 width and GR07 frequency move oppositely for a VDD change)")

    m = rows[0]
    real = m["coh"] > 5 * m["bias"]
    print("\n  " + ("shared noise is resolved above the estimator bias"
                    if real else
                    "coherence is at the estimator bias - no shared noise "
                    "resolved in this run"))
    if not real:
        # A null is only useful as a number. The shared term cannot exceed what
        # the bias would have hidden, which bounds it - and bounding the supply
        # is the point of the experiment even when nothing shows up.
        sel = (f >= band[0]) & (f < band[1])
        lim = max(m["coh"], m["bias"])
        shared = float(np.sqrt(lim * np.median(pxx[sel]))) * 1e6
        total = float(np.sqrt(np.median(pxx[sel]))) * 1e6
        print(f"  upper limit on the shared term: {shared:.1f} ppm/√Hz, "
              f"{shared/total*100:.1f}% of GR06's {total:.0f} ppm/√Hz")
        print(f"  as supply noise that is < {shared*VDD_V:.1f} µV/√Hz on "
              f"{VDD_V:g} V - so the supply is not the source, and GR06's "
              f"noise is local to it")
    if real:
        anti = abs(abs(m["phase_deg"]) - 180.0) < 45.0
        unity = m["gain_lo"] <= 1.0 <= m["gain_hi"]
        if anti and unity:
            print("  anti-phase with |gain| bracketing 1 - matches the supply")
        elif anti:
            print(f"  anti-phase, but |gain| {m['gain_lo']:.2f}-{m['gain_hi']:.2f} "
                  f"excludes 1 - shared, though not purely the supply")
        else:
            print(f"  phase {m['phase_deg']:+.0f}° is not the 180° the supply "
                  f"requires - shared by some other mechanism")

    coh = np.abs(pxy) ** 2 / (pxx * pyy)
    fig, ax = plt.subplots(1, 3, figsize=(14, 4.3))
    a = ax[0]
    a.loglog(f[1:], np.sqrt(pxx[1:]) * 1e6, lw=1.0, color=C6, label="GR06 own+shared")
    a.loglog(f[1:], np.sqrt(pyy[1:]) * 1e6, lw=1.0, color=C7, label="GR07 own+shared")
    a.loglog(f[1:], np.sqrt(np.abs(pxy[1:])) * 1e6, lw=1.4, color=C_COM,
             label="shared (cross)")
    a.set_xlabel("offset frequency  [Hz]"); a.set_ylabel("ppm/√Hz")
    a.set_title("Auto- vs cross-spectrum", fontsize=10); a.legend(fontsize=8)

    a = ax[1]
    a.semilogx(f[1:], coh[1:], lw=1.0, color=C_COM)
    a.axhline(1.0 / max(nseg, 1), ls="--", color=INK2, lw=1.1,
              label=f"estimator bias 1/N, N={nseg}")
    a.axvspan(band[0], band[1], color=INK2, alpha=.07)
    a.set_xlabel("offset frequency  [Hz]"); a.set_ylabel("coherence γ²")
    a.set_ylim(0, 1); a.set_title("How much is shared", fontsize=10)
    a.legend(fontsize=8)

    a = ax[2]
    own6 = pxx * (1 - coh)
    own7 = pyy * (1 - coh)
    a.loglog(f[1:], np.sqrt(own6[1:]) * 1e6, lw=1.1, color=C6, label="GR06 own")
    a.loglog(f[1:], np.sqrt(own7[1:]) * 1e6, lw=1.1, color=C7, label="GR07 own")
    a.loglog(f[1:], np.sqrt((pxx * coh)[1:]) * 1e6, lw=1.4, color=C_COM,
             label="shared")
    a.set_xlabel("offset frequency  [Hz]"); a.set_ylabel("ppm/√Hz")
    a.set_title("Split into shared and own", fontsize=10); a.legend(fontsize=8)

    fig.suptitle("Both sensors reference VDD — what do they share?", y=0.99)
    fig.tight_layout()
    os.makedirs(FIGS, exist_ok=True)
    out = os.path.join(FIGS, "correlated_noise.png")
    fig.savefig(out); plt.close(fig)
    print(f"\n  wrote figs/correlated_noise.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
