#!/usr/bin/env python3
"""Noise spectrum of a long run, against the floors it has to beat.

A PSD on its own cannot answer "is this thermal noise". Thermal noise is white,
and so is the measurement's own quantisation, so the only way the question means
anything is to draw both and see whether the measured floor sits above the
instrument's. This computes:

  * Welch PSD per chunk, averaged - chunks are separate records, so each is
    transformed on its own rather than pretending the seams are not there;
  * the timebase floor, which is the PIO tick quantised over the sample;
  * for GR07 also the retiming floor, one 64 MHz clock cycle per period, which
    is the dominant term and the reason GR07 cannot answer this question;
  * the Allan deviation, which separates white noise (tau^-1/2) from the flicker
    and drift that dominate at long averaging.

Usage:  python3 scripts/noise_analyse.py data/noise-GR06-*.u32
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
import matplotlib                                     # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
from jnwtemp.spectrum import allan_deviation          # noqa: E402

#: GR06 design constants, read out of the schematic and the PDK rather than
#: assumed. The comparator (COMP.sch) has no reference net - only VDD and VSS -
#: so its 0.6 V trip point is set relative to the supply, which is why supply
#: noise enters the pulse width at unity gain.
DESIGN_VREF_V = 0.6
#: One sky130_fd_pr__cap_mim_m3_1 at W=L=5 um. From the PDK model:
#: carea = camimc*w*l = 2.00 fF/um^2 * 25 = 50.0 fF, plus
#: cperim = cpmimc*2*(w+l) = 0.19 fF/um * 20 = 3.8 fF.
DESIGN_C_F = 53.8e-15

FIGS = os.path.join(os.path.dirname(HERE), "data", "figs")
PAPER, INK, INK2, RULE = "#faf7f1", "#1b1811", "#58513f", "#ded5c4"
COLOR = {"GR07": "#2a78d6", "GR06": "#d1521f"}
plt.rcParams.update({
    "figure.facecolor": PAPER, "axes.facecolor": PAPER, "savefig.facecolor": PAPER,
    "font.size": 9.5, "axes.edgecolor": RULE, "axes.labelcolor": INK,
    "text.color": INK, "xtick.color": INK2, "ytick.color": INK2,
    "grid.color": RULE, "axes.grid": True, "grid.alpha": .45,
    "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 130,
})


def load(path):
    meta = json.load(open(os.path.splitext(path)[0] + ".meta.json"))
    ticks = np.fromfile(path, dtype=np.uint32).astype(float)
    per = meta.get("periods_per_sample", 1)
    tick_s = meta["tick_s"]
    if meta["sensor"] == "GR07":
        value = per / (ticks * tick_s)          # Hz
        fs = 1.0 / (per / value.mean())
        unit, label = "Hz", "frequency"
    else:
        value = ticks * tick_s                  # s
        fs = 1.0 / (value.mean() + sum(meta["gr06_pulse_us"]) * 1e-6)
        unit, label = "s", "pulse width"
    # split back into the chunks they were captured in
    bounds, at = [], 0
    for c in meta["chunks"]:
        bounds.append((at, at + c["n"]))
        at += c["n"]
    return meta, value, fs, unit, label, [(a, b) for a, b in bounds if b <= value.size]


def chunked_psd(value, fs, bounds, nperseg):
    """Welch per chunk, averaged by length. Seams are never transformed across."""
    acc = None
    total = 0
    for a, b in bounds:
        seg = value[a:b]
        if seg.size < nperseg:
            continue
        f, p = signal.welch(seg / seg.mean(), fs=fs, nperseg=nperseg,
                            detrend="constant")
        acc = p * seg.size if acc is None else acc + p * seg.size
        total += seg.size
    if acc is None:
        return None, None
    return f, acc / total


def low_band_psd(value, bounds, chunks):
    """Spectrum below what a single chunk can reach, from the chunk means.

    A chunk is only a couple of seconds long and is never transformed across a
    seam, so the per-chunk PSD stops at ~0.5 Hz however long the run is. The
    sequence of chunk means is a second, slower record of the same quantity -
    one sample every couple of seconds over the whole run - and it carries the
    decades the fast record cannot. The two are plotted together and overlap,
    which is also the check that they agree.
    """
    if len(bounds) < 16:
        return None, None
    means = np.array([value[a:b].mean() for a, b in bounds])
    t = np.array([c["t_unix"] for c in chunks[:len(bounds)]])
    dt = float(np.median(np.diff(t)))
    if not np.isfinite(dt) or dt <= 0:
        return None, None
    nper = min(256, means.size // 4 * 2)
    if nper < 8:
        return None, None
    f, p = signal.welch(means / means.mean(), fs=1.0 / dt, nperseg=nper,
                        detrend="constant")
    return f[1:], p[1:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--nperseg", type=int, default=16384)
    args = ap.parse_args()
    path = args.path
    if path is None:
        cand = sorted(glob.glob(os.path.join(os.path.dirname(HERE), "data", "noise-*.u32")))
        if not cand:
            raise SystemExit("no noise-*.u32 runs found")
        path = cand[-1]

    meta, value, fs, unit, label, bounds = load(path)
    s = meta["sensor"]
    dur = sum(c["elapsed_us"] for c in meta["chunks"]) / 1e6
    print(f"{os.path.basename(path)}")
    print(f"  {s}, {value.size:,} samples in {len(bounds)} chunks, "
          f"{dur/60:.1f} min live, duty {meta['duty_cycle']*100:.0f}%")
    print(f"  fs {fs:.1f} Hz -> Nyquist {fs/2:.1f} Hz; "
          f"chunk is {bounds[0][1]-bounds[0][0]:,} samples = "
          f"{(bounds[0][1]-bounds[0][0])/fs:.1f} s -> lowest bin "
          f"{fs/args.nperseg:.4f} Hz")
    mean = value.mean()
    sd = value.std(ddof=1)
    print(f"  mean {mean*(1e9 if unit=='s' else 1e-3):.2f} "
          f"{'ns' if unit=='s' else 'kHz'}, per-sample sigma "
          f"{sd/mean*1e6:.0f} ppm")

    # --- the floors this has to beat
    tick = meta["tick_s"]
    q = tick / np.sqrt(12)
    if s == "GR07":
        per = meta["periods_per_sample"]
        floor_frac = q / (per / mean)            # ticks over the whole bin
        clk = 1.0 / meta["project_clock_hz"]
        retime = clk / np.sqrt(12) / np.sqrt(per) / (1 / mean)
        print(f"  timebase floor  {floor_frac*1e6:8.1f} ppm")
        print(f"  retiming floor  {retime*1e6:8.1f} ppm   <- the 64 MHz clock")
        floors = [("timebase (PIO tick)", floor_frac), ("64 MHz retiming", retime)]
    else:
        floor_frac = q / mean
        print(f"  timebase floor  {floor_frac*1e6:8.1f} ppm "
              f"({sd/mean/floor_frac:.1f}x below the measurement)")
        floors = [("timebase (PIO tick)", floor_frac)]

    f, p = chunked_psd(value, fs, bounds, min(args.nperseg,
                                              bounds[0][1] - bounds[0][0]))
    if f is None:
        raise SystemExit("chunks too short for the requested nperseg")

    fig, ax = plt.subplots(1, 2, figsize=(12.5, 4.8))
    a = ax[0]
    fl, pl = low_band_psd(value, bounds, meta["chunks"])
    if fl is not None:
        a.loglog(fl, np.sqrt(pl) * 1e6, lw=1.3, color=INK2, alpha=.85,
                 label=f"from chunk means ({1/np.median(np.diff([c['t_unix'] for c in meta['chunks']])):.2f} Hz)")
    a.loglog(f[1:], np.sqrt(p[1:]) * 1e6, lw=1.0, color=COLOR[s],
             label=f"{s} within chunks")
    for name, frac in floors:
        lvl = frac * np.sqrt(2 / fs) * 1e6       # white PSD giving that variance
        a.axhline(lvl, ls="--", lw=1.2, color=INK2)
        a.annotate(f"{name}: {lvl:.1f}", (f[1], lvl), fontsize=8,
                   color=INK2, va="bottom")
    a.set_xlabel("offset frequency  [Hz]")
    a.set_ylabel("fractional noise  [ppm/√Hz]")
    a.set_title(f"{s} noise spectrum — is there a white floor above the "
                f"instrument?", fontsize=10)
    a.legend(fontsize=8.5)

    a = ax[1]
    taus, dev = allan_deviation(value / mean, 1.0 / fs)
    good = np.isfinite(dev) & (dev > 0)
    a.loglog(np.asarray(taus)[good], np.asarray(dev)[good] * 1e6, "o-",
             ms=4, lw=1.4, color=COLOR[s])
    tt = np.asarray(taus)[good]
    if tt.size > 2:
        ref = np.asarray(dev)[good][0] * 1e6 * np.sqrt(tt[0] / tt)
        a.loglog(tt, ref, ls="--", lw=1.1, color=INK2,
                 label="τ^(-1/2) — what white noise does")
        a.legend(fontsize=8.5)
    a.set_xlabel("averaging time τ  [s]")
    a.set_ylabel("Allan deviation  [ppm]")
    a.set_title("Averaging down, or not", fontsize=10)

    fig.suptitle(f"{s}: {value.size:,} samples over {dur/60:.0f} min", y=0.99)
    fig.tight_layout()
    os.makedirs(FIGS, exist_ok=True)
    out = os.path.join(FIGS, f"noise_{s}.png")
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote figs/noise_{s}.png")

    # --- what a white floor would mean physically
    #
    # GR06 charges a capacitor with a PTAT current until a comparator trips, so
    # the pulse width is t = V_ref*C/I. Voltage noise at the threshold turns
    # into time noise through the slew rate I/C:
    #
    #     sigma_t = sigma_V / (I/C) = sigma_V * t / V_ref
    #
    # so a fractional width noise sigma_t/t is directly sigma_V/V_ref - it does
    # not depend on the current or the capacitance separately. If that voltage
    # noise is kT/C on the ramp capacitor, sigma_V = sqrt(kT/C), and the
    # measured fraction implies a capacitance. An implausible C means kT/C is
    # not the dominant term and the comparator's own noise is.
    if s == "GR06":
        K_B, T = 1.380649e-23, 300.0
        white = float(np.sqrt(np.median(p[f > fs / 4])))    # fractional, /sqrt(Hz)
        sigma_frac = white * np.sqrt(fs / 2)                # integrated to Nyquist
        print(f"\n  white part integrated to Nyquist: {sigma_frac*1e6:.0f} ppm "
              f"of {sd/mean*1e6:.0f} ppm total")
        # The design is known, so this is a prediction to check, not a fit.
        kt_c = np.sqrt(K_B * T / DESIGN_C_F)
        pred = kt_c / DESIGN_VREF_V
        print(f"  kT/C on the design's {DESIGN_C_F*1e15:.1f} fF: "
              f"sqrt(kT/C) = {kt_c*1e6:.0f} uV over V_ref {DESIGN_VREF_V} V "
              f"-> {pred*1e6:.0f} ppm")
        frac_pow = (pred / sigma_frac) ** 2
        print(f"  that is {frac_pow*100:.1f}% of the measured white power, so "
              f"kT/C is NOT the limit")
        rest = np.sqrt(max(sigma_frac**2 - pred**2, 0.0))
        print(f"  the other {rest*1e6:.0f} ppm is comparator noise, supply "
              f"noise, or both")
        # V_ref tracks VDD, so a fractional width error is a fractional supply
        # error one-for-one - that is what makes the cross-correlation test work.
        for vdd in (1.8,):
            print(f"  as supply noise that would be {rest*vdd*1e3:.1f} mV rms "
                  f"on a {vdd} V rail "
                  f"({white*vdd*1e6:.0f} uV/sqrt(Hz)) - see corr_run.py")

    hi = f > fs / 4
    if hi.any():
        meas_hi = float(np.sqrt(np.median(p[hi])) * 1e6)
        inst = min(fr for _, fr in floors) * np.sqrt(2 / fs) * 1e6
        print(f"\n  high-frequency floor: measured {meas_hi:.1f} ppm/√Hz vs "
              f"instrument {inst:.1f}")
        print("  " + ("above the instrument - a real noise floor is resolved"
                      if meas_hi > 1.5 * inst else
                      "at the instrument floor - this run cannot see past it"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
