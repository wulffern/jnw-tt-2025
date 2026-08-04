#!/usr/bin/env python3
"""Feed corr_analyse.py data with a known answer and check it recovers it.

Cross-correlation analysis is easy to get subtly wrong - a sign slip, an
off-by-one in the alignment, a normalisation that quietly rescales the gain -
and every one of those failures still produces a plausible-looking plot. Bench
time is expensive, so the analysis is checked against fabricated data first,
where the true coherence and the true gain are known by construction.

The fabricated data mimics the real thing: a shared supply term entering GR06's
width at +1 and GR07's frequency at -1, plus independent per-sensor noise, plus
GR07's clock quantisation, written into the same .u32 files with the same
alignment index the board produces.

    python3 scripts/corr_selftest.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

SYSCLK = 128_000_000
PERIODS = 907
F7 = 910e3          # GR07 nominal
W6 = 7027e-9        # GR06 nominal width
CLK = 64e6          # project clock, for GR07's quantisation


def fabricate(dirpath, n_chunks=40, n7=2500, shared_ppm=3000.0,
              own6_ppm=1000.0, own7_ppm=1000.0, seed=1, name="corr-dual-selftest",
              quantise=True):
    """Write .u32 files with a shared term that is genuinely shared.

    The shared process is generated once on the GR06 pulse grid - the finer of
    the two - and GR07 then sees the *mean of it over its own sample window*,
    which is what a slower counter physically does. An earlier version repeated
    the shared term by ceil(pulses-per-sample), so it drifted out of step with
    GR07 and the coherence collapsed. That was a bug in the fake data rather
    than in the analysis, which is exactly the confusion this file exists to
    prevent, so the mechanism is now built from the same index the board emits.
    """
    rng = np.random.default_rng(seed)
    tick7, tick6 = 3 / SYSCLK, 2 / SYSCLK
    per_pulse = W6 + 220e-6
    ratio = (PERIODS / F7) / per_pulse            # GR06 pulses per GR07 sample
    stem = os.path.join(dirpath, name)
    f7h = open(stem + ".gr07.u32", "wb")
    f6h = open(stem + ".gr06.u32", "wb")
    fih = open(stem + ".index.u32", "wb")
    chunks = []
    for c in range(n_chunks):
        idx = np.clip(np.round(np.arange(1, n7 + 1) * ratio), 1, None).astype(np.int64)
        n6 = int(idx[-1])
        # the shared supply term, on the fine (GR06) grid
        s6 = rng.normal(0, shared_ppm * 1e-6, n6)
        # what GR07 sees: the mean of the same process over its sample window
        lo = np.concatenate(([0], idx[:-1]))
        csum = np.concatenate(([0.0], np.cumsum(s6)))
        s7 = (csum[idx] - csum[lo]) / np.maximum(idx - lo, 1)

        w = W6 * (1 + s6 + rng.normal(0, own6_ppm * 1e-6, n6))
        t6 = np.round(w / tick6).astype(np.uint32)
        f = F7 * (1 - s7 + rng.normal(0, own7_ppm * 1e-6, n7))
        if quantise:
            # Real hardware quantises each period independently, so averaging
            # PERIODS of them cuts the quantisation by sqrt(PERIODS). Rounding
            # the mean period once instead would inject the full per-period LSB
            # and make GR07 look 30x noisier than the silicon is.
            lsb = 1.0 / CLK
            jit = rng.normal(0, lsb / np.sqrt(12) / np.sqrt(PERIODS), n7)
            t7f = PERIODS * (1.0 / f + jit) / tick7
        else:
            t7f = PERIODS * (1.0 / f) / tick7
        t7 = np.round(t7f).astype(np.uint32)

        t7.tofile(f7h); t6.tofile(f6h); idx.astype(np.uint32).tofile(fih)
        chunks.append({"t_unix": 1.7e9 + c * 3.0, "n7": int(n7), "n6": int(n6),
                       "elapsed_us": int(n7 * PERIODS / F7 * 1e6)})
    for fh in (f7h, f6h, fih):
        fh.close()
    json.dump({"tool": "corr_selftest", "mode": "dual", "sysclk_hz": SYSCLK,
               "gr07_periods_per_sample": PERIODS, "gr07_tick_s": tick7,
               "gr06_tick_s": tick6, "gr06_pulse_us": [20, 200],
               "k_per_ppm": 1 / 3378.0, "chunks": chunks},
              open(stem + ".meta.json", "w"), indent=1)
    return stem + ".meta.json", ratio


def run(meta):
    out = subprocess.run([sys.executable, os.path.join(HERE, "corr_analyse.py"), meta],
                         capture_output=True, text=True)
    return out.stdout + out.stderr


def parse(text, tag):
    """-> (coherence, gain_lo, gain_hi, phase_deg)"""
    for line in text.splitlines():
        if line.strip().startswith(tag):
            p = line.split()
            rng = p[-2].split("-")
            return (float(p[-4]), float(rng[0]), float(rng[1]),
                    float(p[-1].rstrip("\u00b0")))
    return (None,) * 4


def main() -> int:
    fails = []
    with tempfile.TemporaryDirectory() as d:
        # Case 1: the shared term dominates, so the right answer is unambiguous.
        shared, own6, own7 = 3000.0, 1000.0, 1000.0
        meta, ratio = fabricate(d, shared_ppm=shared, own6_ppm=own6,
                                own7_ppm=own7, name="corr-dual-strong",
                                quantise=False)
        text = run(meta)
        print(text.strip())
        # GR06 is averaged over `ratio` pulses by the alignment, which divides
        # its own noise power by that factor; the shared term survives intact.
        # GR07 sees the *window mean* of the shared process, so the shared
        # power both channels have in common is shared^2/ratio, not shared^2.
        # GR06's own noise is averaged by the same factor.
        ps, p6, p7 = shared**2 / ratio, own6**2 / ratio, own7**2
        exp = ps ** 2 / ((ps + p6) * (ps + p7))
        coh, glo, ghi, ph = parse(text, "measured")
        print(f"\n  expected coherence ~{exp:.3f}, |gain| bracketing 1, phase 180")
        if coh is None:
            fails.append("could not parse the analysis output")
        else:
            if abs(coh - exp) > 0.08:
                fails.append(f"coherence {coh:.3f} vs expected {exp:.3f}")
            if not (glo <= 1.0 <= ghi):
                fails.append(f"gain bracket {glo:.3f}-{ghi:.3f} misses the true 1.0")
            if abs(abs(ph) - 180.0) > 20:
                fails.append(f"phase {ph:.0f} should be 180 (anti-phase)")

        # Case 2: realistic - GR07 carries its clock quantisation. The exact
        # coherence now depends on that, so this checks only what matters:
        # the shared term is still found, still anti-phase, still unity gain,
        # despite GR07's own noise being far larger than the shared term.
        # 200 chunks: a 300 ppm shared term under 2000 ppm of own noise is not
        # resolvable in two minutes, which is itself the useful lesson - this
        # experiment needs a long record, not a quick one.
        meta3, _ = fabricate(d, n_chunks=200, shared_ppm=300.0, own6_ppm=2000.0,
                             own7_ppm=500.0, seed=3, name="corr-dual-real",
                             quantise=True)
        text3 = run(meta3)
        coh3, glo3, ghi3, ph3 = parse(text3, "measured")
        nullc = parse(text3, "time-shift null")[0]
        print(f"  realistic case: coherence {coh3}, phase {ph3}, null {nullc}")
        if coh3 is None or nullc is None:
            fails.append("realistic case produced no parsable result")
        else:
            if coh3 < 2 * nullc:
                fails.append(f"realistic coherence {coh3:.4f} not clear of the "
                             f"null {nullc:.4f}")
            if abs(abs(ph3) - 180.0) > 35:
                fails.append(f"realistic phase {ph3:.0f} lost the anti-phase sign")

        # Case 3: nothing shared at all - coherence must fall to the bias.
        meta2, _ = fabricate(d, shared_ppm=0.0, seed=7, name="corr-dual-null")
        text2 = run(meta2)
        coh2 = parse(text2, "measured")[0]
        print(f"  no-shared-term control: coherence {coh2}")
        if coh2 is None:
            fails.append("null case produced no parsable result")
        elif coh2 > 0.05:
            fails.append(f"coherence {coh2:.3f} with nothing shared - the "
                         f"estimator invents correlation")

    print()
    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("PASS - recovers a known shared term at the right gain and sign, "
          "and finds nothing when nothing is there")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
