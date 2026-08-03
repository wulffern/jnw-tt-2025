#!/usr/bin/env python3
"""Random-telegraph (burst) noise in GR06, from the committed dwell extract.

GR06's rate does not wander continuously - it switches between two discrete
levels, staying in each for a few hundred ms to a few seconds. That is a single
trap capturing and emitting a carrier, and it is the dominant noise source in
this sensor, worth more than half a kelvin.

A warning about method, because the first attempt at this got it wrong. Reaching
for a PSD first is the natural move and it produces a confident, false answer:
detrending with a 5 s Savitzky-Golay filter is a 0.2 Hz high-pass, and the
filter's own corner appears as a spectral peak at 0.3-1 Hz in *both* sensors.
It fits beautifully as a Lorentzian. The tell is that GR07 shows the same peak
while its coherence with GR06 is zero - two independent sensors cannot share a
noise process and be incoherent. RTS is a time-domain claim, so it is settled in
the time domain, and the spectrum is only quoted afterwards.

Usage:  python3 scripts/chamber_burst.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import matplotlib                                            # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                              # noqa: E402
from chamber_analyse import DATA, FIGS, KELVIN, fit_affine   # noqa: E402
from chamber_figures import COLOR, INK2                      # noqa: E402

#: Boltzmann in eV/K, for the activation energy.
K_EV = 8.617333e-5
#: A dwell only counts if its two levels are separated by more than this many
#: multiples of the white noise; above ~55 degC they merge and a threshold just
#: bisects a Gaussian, which would report a fictitious 40 % occupancy.
MIN_SNR = 2.0


def two_state(t, y, med_n=31):
    """Score the low state on a 0.3 s median, so single samples cannot trigger it."""
    yd = y - np.polyval(np.polyfit(t - t.mean(), y, 2), t - t.mean())
    m = signal.medfilt(yd, med_n)
    hi = np.median(m[m > np.median(m)])
    lo = np.median(m[m < np.percentile(m, 8)])
    inlow = m < (hi + lo) / 2
    e = np.diff(np.concatenate([[0], inlow.view(np.int8), [0]]))
    dur = (np.flatnonzero(e == -1) - np.flatnonzero(e == 1)) * 0.01
    dur = dur[dur > 0.05]
    white = float(np.std(np.diff(yd)) / np.sqrt(2))
    return dict(detrended=yd, median=m, step=float(hi - lo), white=white,
                snr=float((hi - lo) / white), frac=float(inlow.mean()),
                dur=float(dur.mean()) if dur.size else np.nan, n=int(dur.size))


def main() -> None:
    os.makedirs(FIGS, exist_ok=True)
    d = pd.read_csv(os.path.join(DATA, "2026-08-03_chamber_dwell.csv"))
    tab = pd.read_csv(os.path.join(DATA, "2026-08-03_chamber_summary.csv"))
    slope, _ = fit_affine(tab.ref_c, tab.GR06_rate_hz)

    st = {}
    for k, g in d.groupby("dwell"):
        st[k] = two_state(g.t_rel_s.to_numpy(), g.GR06_rate_hz.to_numpy())
        st[k]["ref"] = float(g.chamber_actual_c.median())

    r = pd.DataFrame([{**{a: v[a] for a in
                          ("ref", "step", "snr", "frac", "dur", "n")}, "dwell": k}
                      for k, v in st.items()])
    good = r[r.snr > MIN_SNR].dropna(subset=["dur"])
    x = 1.0 / (K_EV * (good.ref + KELVIN))
    slope_ln = np.polyfit(x, np.log(1 / good.dur), 1)[0]
    ea_mev = -slope_ln * 1000
    rho = float(np.corrcoef(x, np.log(1 / good.dur))[0, 1])

    print(f"step {r.step.mean():.0f} Hz = {r.step.mean()/slope*1000:.0f} mK mean")
    print(f"resolved dwells: {len(good)} of {len(r)}")
    print(f"Ea = {ea_mev:.0f} meV, r = {rho:+.3f}")

    show = [k for k in (2, 5, 9, 12) if k in st]
    fig = plt.figure(figsize=(12.5, 10.6), constrained_layout=True)
    gs = fig.add_gridspec(len(show) + 1, 2, width_ratios=[2.3, 1],
                          height_ratios=[1] * len(show) + [1.35], hspace=0.30,
                          wspace=0.10)
    for i, k in enumerate(show):
        v = st[k]
        t = d[d.dwell == k].t_rel_s.to_numpy()
        t = t - t[0]
        ax = fig.add_subplot(gs[i, 0])
        ax.plot(t, v["detrended"], lw=0.4, color=COLOR["GR06"], alpha=0.55)
        ax.plot(t, v["median"], lw=1.5, color=INK2)
        ax.set_ylabel(f"{v['ref']:.0f} °C\n[Hz]")
        if i == 0:
            ax.set_title("GR06 rate, dwell drift removed · dark line is a 0.3 s median",
                         fontsize=9.5)
        if i == len(show) - 1:
            ax.set_xlabel("time within the dwell  [s]")
        ax = fig.add_subplot(gs[i, 1])
        ax.hist(v["detrended"], bins=90, color=COLOR["GR06"], alpha=0.85)
        ax.set_yticks([])
        if i == 0:
            ax.set_title("distribution — two lobes, not one", fontsize=10)
        if i == len(show) - 1:
            ax.set_xlabel("detrended rate  [Hz]")

    ax = fig.add_subplot(gs[len(show), 0])
    ax.semilogy(good.ref, good.dur * 1000, "o", color=COLOR["GR06"], ms=7)
    xs = np.linspace(good.ref.min(), good.ref.max(), 50)
    fit = np.polyfit(x, np.log(1 / good.dur), 1)
    ax.semilogy(xs, 1000 / np.exp(np.polyval(fit, 1 / (K_EV * (xs + KELVIN)))),
                "-", color=INK2, lw=1.5,
                label=f"Arrhenius, Eₐ = {ea_mev:.0f} meV  (r = {rho:+.2f})")
    ax.set_xlabel("chamber reference  [°C]")
    ax.set_ylabel("mean burst\nlifetime  [ms]")
    ax.set_title("The trap empties faster as it warms — which is what makes it a trap",
                 fontsize=10)
    ax.legend(fontsize=9)

    ax = fig.add_subplot(gs[len(show), 1])
    ax.plot(r.ref, r.step / slope * 1000, "o-", color=COLOR["GR06"], ms=5, lw=1.4)
    ax.axvspan(55, 72, color=INK2, alpha=0.10)
    ax.annotate("levels merge\ninto the noise", (63, ax.get_ylim()[1] * 0.72),
                ha="center", fontsize=8, color=INK2)
    ax.set_xlabel("chamber reference  [°C]")
    ax.set_ylabel("step  [mK]")
    ax.set_title("Amplitude of the jump", fontsize=10)

    fig.suptitle("GR06 has random-telegraph noise: two discrete levels about "
                 f"{r.step.mean()/slope*1000:.0f} mK apart", fontsize=13)
    # constrained_layout already placed everything; tight_layout would undo it
    path = os.path.join(FIGS, "gr06_burst.png")
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote figs/gr06_burst.png  ({os.path.getsize(path)/1024:.0f} kB)")


if __name__ == "__main__":
    main()
