#!/usr/bin/env python3
"""Figures from the climate-chamber characterisation run.

Writes PNGs into data/figs/. Run chamber_analyse.py first for the numbers.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from chamber_analyse import (  # noqa: E402
    CLK_HZ, COLOR, DATA, FIGS, KELVIN, RUN, SENSORS,
    fit_affine, invert, load, one_point, settled_table,
)

INK, INK2, RULE, PAPER = "#1b1811", "#58513f", "#ded5c4", "#faf7f1"

plt.rcParams.update({
    "figure.facecolor": PAPER, "axes.facecolor": PAPER,
    "savefig.facecolor": PAPER, "font.size": 10,
    "axes.edgecolor": RULE, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK2, "ytick.color": INK2, "grid.color": RULE,
    "axes.grid": True, "grid.alpha": 0.5, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.dpi": 130, "legend.frameon": False,
})


def save(fig, name):
    p = os.path.join(FIGS, name)
    fig.tight_layout()
    fig.savefig(p)
    plt.close(fig)
    print(f"  wrote {os.path.relpath(p, DATA)}  ({os.path.getsize(p)/1024:.0f} kB)")


# --------------------------------------------------------------- 1. transfer
def fig_transfer(tab):
    """Small multiples: the two rates differ 6x, so one shared axis hides both."""
    fig = plt.figure(figsize=(9, 7.2))
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.15], hspace=0.42, wspace=0.28)
    for i, sname in enumerate(SENSORS):
        ax = fig.add_subplot(gs[0, i])
        ax.plot(tab.ref_c, tab[f"{sname}_rate"] / 1e3, "o-",
                color=COLOR[sname], ms=4.5, lw=1.6)
        a, _ = fit_affine(tab.ref_c, tab[f"{sname}_rate"])
        ax.set_title(f"{sname}   {a/1e3:.2f} kHz/K", color=COLOR[sname], fontsize=10)
        ax.set_ylabel("rate  [kHz]")
        ax.set_xlabel("reference  [°C]")

    ax = fig.add_subplot(gs[1, :])
    for sname in SENSORS:
        a, b = fit_affine(tab.ref_c, tab[f"{sname}_rate"])
        resid = (tab[f"{sname}_rate"] - (a * (tab.ref_c + KELVIN) + b)) / a
        ax.plot(tab.ref_c, resid, "o-", color=COLOR[sname], ms=5, lw=1.8,
                label=f"{sname}   peak {np.abs(resid).max():.2f} K")
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_ylabel("INL  [K]")
    ax.set_xlabel("chamber reference temperature  [°C]")
    ax.set_title("Integral nonlinearity — residual after the best straight line "
                 "in absolute temperature", fontsize=10)
    ax.legend()
    fig.suptitle("Transfer against the chamber reference, 5–70 °C", y=0.98)
    save(fig, "transfer_inl.png")


# ------------------------------------------------------- 2. calibration error
def _quad_invert(coeffs, rates, tk_mid):
    out = []
    for rr in np.atleast_1d(rates):
        roots = np.roots([coeffs[0], coeffs[1], coeffs[2] - rr])
        roots = roots[np.isreal(roots)].real
        out.append((min(roots, key=lambda x: abs(x - tk_mid)) - KELVIN)
                   if roots.size else np.nan)
    return np.array(out)


def calibration_models(ref, rate):
    """The calibrations someone might actually perform, cheapest first."""
    tk = ref + KELVIN
    i25 = int(np.argmin(np.abs(ref - 25)))
    i20, i60 = int(np.argmin(np.abs(ref - 20))), int(np.argmin(np.abs(ref - 60)))
    idx3 = [int(np.argmin(np.abs(ref - t))) for t in (10, 40, 65)]
    out = {}
    a, b = one_point(ref[i25], rate[i25])
    out[f"1-point @ {ref[i25]:.0f} °C"] = invert(a, b, rate) - ref
    a, b = fit_affine([ref[i20], ref[i60]], [rate[i20], rate[i60]])
    out[f"2-point {ref[i20]:.0f}/{ref[i60]:.0f} °C"] = invert(a, b, rate) - ref
    c3 = np.polyfit(tk[idx3], rate[idx3], 2)
    out[f"3-point quad {ref[idx3[0]]:.0f}/{ref[idx3[1]]:.0f}/{ref[idx3[2]]:.0f}"] = \
        _quad_invert(c3, rate, tk.mean()) - ref
    # Nothing above three points. Fitting all 14 chamber set points measures how
    # well a polynomial can chase this particular part; it is not a calibration
    # anyone would run in production, and quoting its residual flatters the
    # sensor. Three trim points is already at the expensive end of realistic.
    return out


def fig_calerror(tab):
    ref = tab.ref_c.to_numpy()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), sharey=True)
    styles = ["-", "--", "-."]
    for ax, sname in zip(axes, SENSORS):
        models = calibration_models(ref, tab[f"{sname}_rate"].to_numpy())
        for (name, err), ls in zip(models.items(), styles):
            ax.plot(ref, err, ls=ls, marker="o", ms=3.5, lw=1.6, color=COLOR[sname],
                    alpha=0.95 if ls == "-" else 0.6,
                    label=f"{name}  ·  max {np.abs(err).max():.2f} K")
        ax.axhline(0, color=INK2, lw=0.8)
        ax.set_title(sname, color=COLOR[sname])
        ax.set_xlabel("chamber reference  [°C]")
        ax.legend(fontsize=8)
    axes[0].set_ylabel("reading − reference  [K]")
    fig.suptitle("Calibration error against an external reference", y=0.99)
    save(fig, "calibration_error.png")


# ------------------------------------------------------------------ 3. noise
def fig_noise(tab):
    fig, ax = plt.subplots(2, 1, figsize=(8, 6.2), sharex=True,
                           gridspec_kw={"height_ratios": [1, 1]})
    per = 1e9 / tab.GR07_rate.to_numpy()
    cyc = per / (1e9 / CLK_HZ)
    edge = np.minimum(cyc % 1, 1 - cyc % 1)
    for s in SENSORS:
        a, _ = fit_affine(tab.ref_c, tab[f"{s}_rate"])
        sig_mk = tab[f"{s}_sigma_hz"] / a * 1000
        ax[0].plot(tab.ref_c, sig_mk, "o-", color=COLOR[s], ms=5, lw=1.6, label=s)
    ax[0].set_ylabel("noise on one 10 ms point  [mK]")
    ax[0].set_title("Noise is not a constant — and for GR07 it tracks the clock")
    ax[0].legend()

    ax[1].plot(tab.ref_c, edge, "o-", color=COLOR["GR07"], ms=5, lw=1.6)
    ax[1].set_ylabel("distance to a whole\nclock cycle  [cycles]")
    ax[1].set_xlabel("chamber reference temperature  [°C]")
    r = np.corrcoef(edge, tab.GR07_sigma_hz)[0, 1]
    ax[1].set_title(f"GR07 period vs the 64 MHz grid — correlation with its noise "
                    f"r = {r:+.3f}", fontsize=10)
    for x, y, c in zip(tab.ref_c, edge, cyc):
        ax[1].annotate(f"{c:.2f}", (x, y), textcoords="offset points",
                       xytext=(0, 7), ha="center", fontsize=7, color=INK2)
    save(fig, "noise_vs_temperature.png")


# --------------------------------------------------------------- 4. settling
def _peak_gap(tt, d, bin_s=1.0):
    """Largest |die - air| the ramp actually sustains, averaged over 1 s."""
    if tt.size == 0:
        return np.nan
    b = np.floor((tt - tt[0]) / bin_s).astype(int)
    ok = np.isfinite(d)
    if not ok.any():
        return np.nan
    sums = np.bincount(b[ok], weights=d[ok])
    cnt = np.bincount(b[ok])
    return float(np.nanmax(np.abs(sums[cnt > 0] / cnt[cnt > 0])))


def fig_settling(df, tab):
    """The die against the chamber air - the sensors' own lag, not the oven's."""
    segs = []
    for seg, g in df.groupby("segment"):
        if g.t_rel_s.iloc[-1] - g.t_rel_s.iloc[0] > 200 and g.chamber_on.max() == 1:
            segs.append(seg)
    a7, b7 = fit_affine(tab.ref_c, tab.GR07_rate)
    a6, b6 = fit_affine(tab.ref_c, tab.GR06_rate)

    fig = plt.figure(figsize=(11, 4.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.25, 1], wspace=0.26)
    ax = fig.add_subplot(gs[0, 0])
    show = segs[len(segs) // 2]
    g = df[df.segment == show]
    t = (g.t_rel_s - g.t_rel_s.iloc[0]).to_numpy() / 60
    def norm(y, tt=None):
        # windows in seconds, so this is independent of the sample rate
        tt = t * 60 if tt is None else tt
        y0 = np.median(y[tt <= tt[0] + 3.0])
        y1 = np.median(y[tt >= tt[-1] - 30.0])
        return (y - y0) / (y1 - y0) if abs(y1 - y0) > 1e-9 else y * 0
    ax.plot(t, norm(g.chamber_actual_c.to_numpy()), color=INK2, lw=2.2,
            label="chamber air (its own sensor)")
    ax.plot(t, norm(invert(a7, b7, g.GR07_rate_hz.to_numpy())), color=COLOR["GR07"],
            lw=1.0, alpha=0.85, label="GR07 (die)")
    ax.plot(t, norm(invert(a6, b6, g.GR06_rate_hz.to_numpy())), color=COLOR["GR06"],
            lw=1.0, alpha=0.7, label="GR06 (die)")
    ax.axhline(0.632, color=INK2, lw=0.7, ls="--")
    ax.set_xlabel("time since set-point change  [min]")
    ax.set_ylabel("normalised response")
    ax.set_title(f"One {g.chamber_set_c.iloc[0]:.0f} °C step — the die follows the air",
                 fontsize=10)
    ax.legend(fontsize=8.5)

    # How far the die departs from the air while the oven slews. A *time* lag
    # is not measurable here - the chamber reports every ~2 s in 0.1 K steps, so
    # anything faster than that is below the reference's resolution - but the
    # temperature gap during the ramp is in kelvin and is perfectly visible.
    ax = fig.add_subplot(gs[0, 1])
    sets, gap7, gap6, t63 = [], [], [], []
    for seg in segs:
        g = df[df.segment == seg]
        tt = (g.t_rel_s - g.t_rel_s.iloc[0]).to_numpy()
        air = g.chamber_actual_c.to_numpy()
        d7 = invert(a7, b7, g.GR07_rate_hz.to_numpy()) - air
        d6 = invert(a6, b6, g.GR06_rate_hz.to_numpy()) - air
        ramp = tt < 240
        sets.append(float(g.chamber_set_c.iloc[0]))
        # peak of a 1 s average, not of a raw 10 ms sample: a bare max would
        # otherwise report the sensor noise floor and would change with the
        # sample rate, so the raw run and the 1 s extract would disagree.
        gap7.append(_peak_gap(tt[ramp], d7[ramp]))
        gap6.append(_peak_gap(tt[ramp], d6[ramp]))
        n = norm(air, tt)
        i = np.flatnonzero(n > 0.632)
        t63.append(float(tt[i[0]]) if i.size else np.nan)
    ax.plot(sets, gap7, "o-", color=COLOR["GR07"], ms=5, lw=1.6,
            label=f"GR07  median {np.nanmedian(gap7):.1f} K")
    ax.plot(sets, gap6, "o-", color=COLOR["GR06"], ms=5, lw=1.6,
            label=f"GR06  median {np.nanmedian(gap6):.1f} K")
    ax.set_xlabel("chamber set point  [°C]")
    ax.set_ylabel("peak |die − air| during the ramp  [K]")
    ax.set_title(f"How far the die departs from the air while slewing\n"
                 f"(the oven needs {np.nanmedian(t63)/60:.1f} min to 63 %)", fontsize=10)
    ax.legend(fontsize=8.5)
    save(fig, "settling.png")
    return np.nanmedian(t63), np.nanmedian(gap7), np.nanmedian(gap6)


# -------------------------------------------------------------------- 5. DNL
def code_boundaries(code, ruler):
    """Temperature of each code transition, using a continuous sensor as ruler.

    Codes are found by where the quantised value steps, not by how long it
    lingers: dither makes codes overlap, so a boundary is the midpoint between
    the highest ruler value still in code n and the lowest in code n+1.
    """
    order = np.argsort(ruler)
    c, t = code[order], ruler[order]
    edges = {}
    for k in np.unique(c):
        m = c == k
        if m.sum() < 500:
            continue
        edges[int(k)] = (np.percentile(t[m], 2), np.percentile(t[m], 98))
    keys = sorted(edges)
    bounds = {}
    for lo, hi in zip(keys, keys[1:]):
        if hi - lo != 1:
            continue
        bounds[lo] = 0.5 * (edges[lo][0] + edges[hi][1]) if edges[lo][0] > edges[hi][1] \
            else 0.5 * (edges[lo][1] + edges[hi][0])
    return bounds


def fig_dnl(df, tab, pre=None):
    if pre is not None:
        return _plot_dnl(pre.code.to_numpy(), pre.width_k.to_numpy())
    d = df[df.chamber_on == 1]
    per_ns = 1e9 / d.GR07_rate_hz.to_numpy()
    code = np.round(per_ns / (1e9 / CLK_HZ))
    a6, b6 = fit_affine(tab.ref_c, tab.GR06_rate)
    ruler = invert(a6, b6, d.GR06_rate_hz.to_numpy())
    ok = np.isfinite(code) & np.isfinite(ruler)
    bounds = code_boundaries(code[ok], ruler[ok])

    ks = sorted(bounds)
    widths, codes = [], []
    for lo, hi in zip(ks, ks[1:]):
        if hi - lo != 1:
            continue
        # width of code `hi` = distance between the boundaries either side of it
        codes.append(hi)
        widths.append(abs(bounds[lo] - bounds[hi]))
    codes, widths = np.array(codes), np.array(widths)
    # The sweep stopped at 5 and 70 degC, so the outermost codes were only
    # partly traversed and their widths are truncated, not narrow.
    if codes.size > 4:
        codes, widths = codes[1:-1], widths[1:-1]
    if widths.size == 0:
        print("  (not enough fully-traversed codes for DNL)")
        return codes, widths, widths
    return _plot_dnl(codes, widths)


def _plot_dnl(codes, widths):
    lsb = float(np.median(widths))
    dnl = widths / lsb - 1.0

    fig, ax = plt.subplots(2, 1, figsize=(8.4, 6.2), sharex=True)
    ax[0].bar(codes, widths, color=COLOR["GR07"], width=0.75)
    ax[0].axhline(lsb, color=INK2, lw=1, ls="--",
                  label=f"median code width (1 LSB) = {lsb:.2f} K")
    ax[0].set_ylabel("code width  [K]")
    ax[0].set_title("GR07 code = period in whole 64 MHz clock cycles\n"
                    "only fully-traversed codes shown", fontsize=10)
    ax[0].legend(fontsize=8.5)
    ax[1].bar(codes, dnl, color=COLOR["GR07"], width=0.75)
    ax[1].axhline(0, color=INK2, lw=0.8)
    for lim in (0.5, -0.5):
        ax[1].axhline(lim, color=INK2, lw=0.7, ls=":")
    ax[1].set_ylabel("DNL  [LSB]")
    ax[1].set_xlabel("code  (clock cycles per period)")
    ax[1].set_title(f"Differential nonlinearity, GR06 as the ruler — "
                    f"peak {np.abs(dnl).max():.2f} LSB over {codes.size} codes",
                    fontsize=10)
    save(fig, "dnl.png")
    return codes, widths, dnl


# --------------------------------------------------- 6. dense INL from ramps
#: Keep only slowly-moving data. The die follows the air with tau ~ 1 s, so at
#: this rate the lag error is under 0.03 K - negligible against a 1-2 K INL -
#: while the fast initial cool-down carries a ~0.7 K offset that is almost
#: certainly the chamber's own probe lagging, not the die.
MAX_RAMP_K_PER_S = 0.017      # ~1 K/min
REF_BIN_K = 0.25


def dense_inl(df, tab, sensor):
    d = df[df.chamber_on == 1]
    t = d.t_rel_s.to_numpy()
    air = d.chamber_actual_c.to_numpy()
    w = 3001
    air_s = np.convolve(air, np.ones(w) / w, mode="same")
    rate = np.gradient(air_s, t)
    sel = np.ones(len(d), bool)
    sel[:w] = False
    sel[-w:] = False
    sel &= np.abs(rate) < MAX_RAMP_K_PER_S
    sel &= rate > -0.001          # up-ramps and plateaus; see fig caption

    r = d[f"{sensor}_rate_hz"].to_numpy()
    binned = (np.round(air_s[sel] / REF_BIN_K) * REF_BIN_K)
    g = (pd.DataFrame({"T": binned, "r": r[sel]})
         .groupby("T").agg(r=("r", "mean"), n=("r", "size")))
    g = g[g.n > 200]
    a, b = fit_affine(tab.ref_c, tab[f"{sensor}_rate"])
    inl = (g.r.to_numpy() - (a * (g.index.to_numpy() + KELVIN) + b)) / a
    return g.index.to_numpy(), inl, g.n.to_numpy()


def fig_inl_dense(df, tab):
    """Dense INL from the ramps - and why it does not replace dwell points."""
    fig, axes = plt.subplots(2, 1, figsize=(9.2, 7.0), sharex=True)
    for ax, sname in zip(axes, SENSORS):
        T, inl, n = dense_inl(df, tab, sname)
        ax.plot(T, inl, "-", color=COLOR[sname], lw=1.3, alpha=0.75,
                label=f"{len(T)} bins from up-ramps ≤1 K/min")
        a, b = fit_affine(tab.ref_c, tab[f"{sname}_rate"])
        sp = (tab[f"{sname}_rate"] - (a * (tab.ref_c + KELVIN) + b)) / a
        ax.plot(tab.ref_c, sp, "o-", color=INK2, ms=6, mfc="none", mew=1.6, lw=1.2,
                label="14 settled dwell points")
        rough = np.mean(np.abs(np.diff(inl)))
        ax.axhline(0, color=INK2, lw=0.8)
        ax.set_ylabel("INL  [K]")
        ax.set_title(f"{sname}   —   bin-to-bin scatter {rough*1000:.0f} mK, "
                     f"vs {tab[f'{sname}_sigma_hz'].median()/abs(a)*1000/np.sqrt(3000):.0f} mK "
                     f"expected from noise", color=COLOR[sname], fontsize=10)
        ax.legend(fontsize=8.5)
    axes[1].set_xlabel("chamber reference temperature  [°C]")
    fig.suptitle("The ramps add points but not information — the extra structure is "
                 "locked to the 5 K\nset-point spacing, and is the chamber's, not the "
                 "sensors'", y=0.99, fontsize=10.5)
    save(fig, "inl_dense.png")


EXTRACT = os.path.join(DATA, "2026-08-03_chamber")


def load_reduced():
    """Everything the figures need, from the committed extract alone.

    The raw run is 78 MB and is deliberately not in the repo, so DNL and the
    dense ramp INL - the two things that need the sweep at full rate - come
    precomputed or not at all.
    """
    tab = pd.read_csv(f"{EXTRACT}_summary.csv").rename(
        columns={f"{s}_rate_hz": f"{s}_rate" for s in SENSORS})
    return load(f"{EXTRACT}_trace.csv"), tab, pd.read_csv(f"{EXTRACT}_dnl.csv")


def main():
    os.makedirs(FIGS, exist_ok=True)
    arg = sys.argv[1] if len(sys.argv) > 1 else RUN
    pre = None
    if os.path.exists(arg):
        print(f"loading {os.path.basename(arg)} …")
        df = load(arg)
        tab = settled_table(df)
    else:
        print("raw run not present — using the committed extract")
        df, tab, pre = load_reduced()
    print("figures:")
    fig_transfer(tab)
    fig_calerror(tab)
    fig_noise(tab)
    t63, l7, l6 = fig_settling(df, tab)
    if pre is None:
        fig_inl_dense(df, tab)      # needs the ramps at full rate
    codes, widths, dnl = fig_dnl(df, tab, pre)
    print(f"\nsettling: chamber {t63/60:.1f} min to 63 %; peak die-air gap while "
          f"slewing {l7:.1f} K (GR07) / {l6:.1f} K (GR06)")
    print(f"\nDNL: {np.isfinite(widths).sum()} usable codes, "
          f"LSB {np.nanmedian(widths):.2f} K, peak DNL {np.nanmax(np.abs(dnl)):.2f} LSB")


if __name__ == "__main__":
    main()
