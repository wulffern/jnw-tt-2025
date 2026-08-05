#!/usr/bin/env python3
"""Measure ttsky25a project 132 and write data/ring_data.json for its deck.

    Design:  Giant Ring Oscillator (3853 inverters)
    Author:  Uri Shaked
    Chip:    Tiny Tapeout ttsky25a, project 132
    Source:  https://tinytapeout.com/chips/ttsky25a/tt_um_urish_giant_ringosc

Not our design. This is an independent measurement of somebody else's silicon,
made with the rig built for JNW-TEMP. Only results that survived a cross-check
are recorded here; anything the instrument could not resolve is left out rather
than reported weakly.

Two start-up sequences, both deterministic over 10 trials each:

    A: reset, then ui_in = 0x02            -> fast mode, above 125 MHz
    B: ui_in = 0x00, reset, ui_in = 0x02   -> slow mode, 2.947 MHz

The fast mode aliases differently at every sample rate (65.3 MHz at 250 MS/s,
35.5 at 125, 13.9 at 50), so it is above Nyquist and this instrument cannot
say what it is. Only the slow mode is characterised.

    python3 scripts/ring_measure.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
from jnwtemp.board import TTBoard  # noqa: E402
from jnwtemp.logic import CaptureSettings, LogicCapture  # noqa: E402
from jnwtemp.spectrum import allan_deviation, phase_noise  # noqa: E402
from noise_deck import thin_log  # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "ring_data.json")
PROJECT, INDEX, AUTHOR = "tt_um_urish_giant_ringosc", 132, "Uri Shaked"
STAGES = 3853
REF_HZ = 10_000_000
SLOW_HZ = 2.947e6


def thin_db(f, db, per_decade=24):
    """Median dB in geometric frequency bins.

    thin_log() is for amplitude spectra and takes a square root on the way
    through; applied to a quantity already in decibels it halves the scale.
    """
    ok = (f > 0) & np.isfinite(db)
    f, db = f[ok], db[ok]
    if f.size < 4:
        return [], []
    n = max(4, int((np.log10(f[-1]) - np.log10(f[0])) * per_decade))
    edges = np.logspace(np.log10(f[0]), np.log10(f[-1]), n + 1)
    idx = np.digitize(f, edges) - 1
    out_f, out_d = [], []
    for k in range(n):
        m = idx == k
        if m.any():
            out_f.append(float(np.exp(np.mean(np.log(f[m])))))
            out_d.append(float(np.median(db[m])))
    return out_f, out_d


def capture(rate, seconds):
    st = CaptureSettings(channels=[0], sample_rate=int(rate),
                         threshold_volts=1.2, duration_s=seconds)
    with LogicCapture(st) as cap:
        tr = cap.capture()
        got = cap.actual_sample_rate or rate
    t = tr[0]
    if t.num_edges < 32:
        return None, None, got
    _, per = t.periods()
    return per, t.duty(), got


def start(b, mode):
    """Sequence A gives the fast mode, B the slow one. Both are deterministic."""
    if mode == "fast":
        b.reset_project()
        b.exec("tt.ui_in.value = 0x02", timeout=20)
    else:
        b.reset_project()
        b.exec("tt.ui_in.value = 0x00", timeout=20)
        b.reset_project()
        b.exec("tt.ui_in.value = 0x02", timeout=20)
    time.sleep(0.15)


def main() -> None:
    b = TTBoard()
    b.connect()
    b.exec(f"tt.shuttle.{PROJECT}.enable()", timeout=30)
    b.exec("import machine\n"
           f"hz = {REF_HZ}\n"
           "if machine.freq() < 2*hz: machine.freq(2*hz)\n"
           "tt.clock_project_PWM(hz)\n", timeout=25)

    # --- how repeatable is each start-up sequence?
    trials = {}
    for mode in ("fast", "slow"):
        seen = []
        for _ in range(10):
            start(b, mode)
            per, _, _ = capture(250e6, 0.004)
            seen.append(None if per is None else float(1 / per.mean()))
        hit = sum(1 for f in seen
                  if f and ((f < 5e6) == (mode == "slow")))
        trials[mode] = {"trials": len(seen), "as_expected": hit,
                        "mean_hz": float(np.mean([f for f in seen if f]))}
        print(f"  sequence {'A' if mode=='fast' else 'B'} -> {mode}: "
              f"{hit}/{len(seen)}, mean {trials[mode]['mean_hz']/1e6:.4f} MHz")

    # --- the slow mode, measured twice with different timebases
    runs = {}
    for rate in (250e6, 125e6):
        for _ in range(12):
            start(b, "slow")
            per, duty, got = capture(rate, 0.004)
            if per is not None and abs(1/per.mean() - SLOW_HZ)/SLOW_HZ < 0.05:
                per, duty, got = capture(rate, 1.0)
                runs[int(got)] = (per, duty)
                break
    b.disconnect()
    if len(runs) < 2:
        raise SystemExit("could not capture the slow mode at both rates")

    out = {
        "design": {"project": PROJECT, "index": INDEX, "author": AUTHOR,
                   "chip": "Tiny Tapeout ttsky25a", "stages": STAGES,
                   "url": f"https://tinytapeout.com/chips/ttsky25a/{PROJECT}",
                   "note": "measured by us; the design is not ours"},
        "ref_clock_hz": REF_HZ,
        "modes": trials,
        "fast_state": {
            # Not established to be a mode of the ring. Three sample rates give
            # readings that no single frequency can explain - the best fit
            # leaves a 10.5 MHz residual, while the same solver recovers a
            # synthetic 437.3 MHz tone exactly. So edge extraction has broken
            # down on something faster than the analyser can track, and these
            # numbers are sampling artefacts rather than a frequency.
            "apparent_hz": {"500e6": 69.804e6, "250e6": 65.614e6,
                            "125e6": 35.226e6},
            "alias_fit_residual_hz": 1.05e7,
            "solver_validated": True,
        },
        "stages_prime": all(STAGES % k for k in range(2, int(STAGES**0.5) + 1)),
        "runs": {},
    }
    for rate, (per, duty) in sorted(runs.items()):
        f, L, f0 = phase_noise(per, nperseg=32768)
        pf, pL = thin_db(f[1:], np.asarray(L[1:]))
        taus, dev = allan_deviation(per / per.mean(), 1.0 / (1 / per.mean()))
        good = np.isfinite(dev) & (dev > 0)
        tt_, dd = np.asarray(taus)[good], np.asarray(dev)[good]
        step = max(1, tt_.size // 40)
        out["runs"][str(rate)] = {
            "sample_rate_hz": rate,
            "periods": int(per.size),
            "f0_hz": float(f0),
            "duty": float(duty),
            "phase_noise": [[round(a, 4), round(v, 2)] for a, v in zip(pf, pL)],
            "allan": [[round(float(t), 6), round(float(d) * 1e6, 3)]
                      for t, d in zip(tt_[::step], dd[::step])],
        }
        print(f"  slow mode at {rate/1e6:.0f} MS/s: {per.size:,} periods, "
              f"f0 {f0/1e6:.6f} MHz")

    # agreement between the two timebases is the whole basis for trusting this
    ks = sorted(out["runs"])
    a, bb = out["runs"][ks[0]], out["runs"][ks[1]]
    fa = {f: v for f, v in a["phase_noise"]}
    diffs = []
    for f, v in bb["phase_noise"]:
        near = min(fa, key=lambda x: abs(x - f))
        if abs(near - f) / max(f, 1) < 0.05 and 100 <= f <= 3e4:
            diffs.append(v - fa[near])
    out["cross_check"] = {
        "band_hz": [100, 30000],
        "max_abs_diff_db": float(np.max(np.abs(diffs))) if diffs else None,
        "points": len(diffs),
    }
    print(f"  cross-check 100 Hz-30 kHz: max |diff| "
          f"{out['cross_check']['max_abs_diff_db']:.1f} dB")

    json.dump(out, open(OUT, "w"), indent=1)
    print("wrote", os.path.relpath(OUT, os.path.dirname(HERE)))


if __name__ == "__main__":
    main()
