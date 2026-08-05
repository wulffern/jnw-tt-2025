"""Compare the two sensors over the logged run and emit a compact JSON for the deck."""
import csv, json, math, os, sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from jnwtemp.spectrum import allan_deviation

SCR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
CSV = os.path.join(SCR, "2026-07-31_dual_15min.csv")
CAP = os.path.join(SCR, "capture.parquet")
OUT = os.path.join(SCR, "deck_data.json")

rows = list(csv.DictReader(open(CSV)))
col = lambda name: np.array([float(r[name]) for r in rows])
t = col("t_s")
res = {"n_readings": len(rows), "span_s": float(t[-1]), "sensors": {}}

for k in ("GR07", "GR06"):
    T = col(f"{k}_temp_c")
    sem = col(f"{k}_sem_c")
    sig = col(f"{k}_sigma_c")
    n = col(f"{k}_n")
    obs = col(f"{k}_observable_ns")
    rej = col(f"{k}_rejected")
    dt = float(np.mean(np.diff(t)))
    taus, devs = allan_deviation(T, dt)
    # Allan deviation at ~10 s and ~60 s of averaging
    def at(target):
        if taus.size == 0:
            return None
        i = int(np.argmin(np.abs(taus - target)))
        return {"tau_s": float(taus[i]), "dev_c": float(devs[i])}
    res["sensors"][k] = {
        "observable_ns": float(obs.mean()),
        "events_per_capture": float(n.mean()),
        "rejected_total": float(rej.sum()),
        "mean_c": float(T.mean()),
        "reading_std_c": float(T.std(ddof=1)),
        "peak_to_peak_c": float(T.max() - T.min()),
        "sem_c": float(sem.mean()),
        "per_event_sigma_c": float(sig.mean()),
        "allan": {"short": at(10.0), "long": at(60.0)},
        "allan_curve": [[float(a), float(b)] for a, b in zip(taus, devs)],
    }

a, b = col("GR07_temp_c"), col("GR06_temp_c")
res["correlation"] = float(np.corrcoef(a, b)[0, 1])
res["mean_offset_c"] = float((b - a).mean())
res["diff_std_c"] = float((b - a).std(ddof=1))
# How much of each sensor's wander is shared? Compare the spread of the
# difference against the spread of each series.
# Is the wander shared between the two sensors, or independent? If independent,
# the variance of the difference equals the sum of the variances.
res["independence"] = {
    "quadrature_sum": float(math.hypot(a.std(ddof=1), b.std(ddof=1))),
    "diff_std": float((b - a).std(ddof=1)),
    "corr_gr07_time": float(np.corrcoef(a, t)[0, 1]),
    "corr_gr06_time": float(np.corrcoef(b, t)[0, 1]),
    "gr07_slope_mk_per_min": float(np.polyfit(t / 60.0, a, 1)[0] * 1000),
    "gr06_slope_mk_per_min": float(np.polyfit(t / 60.0, b, 1)[0] * 1000),
}
res["common_mode_note"] = {
    "gr07_std": float(a.std(ddof=1)),
    "gr06_std": float(b.std(ddof=1)),
    "diff_std": float((b - a).std(ddof=1)),
}

# Downsample the time series for plotting (keep the shape, bound the payload).
MAXPTS = 240
step = max(1, len(t) // MAXPTS)
res["series"] = {
    "t_min": [round(float(x) / 60.0, 4) for x in t[::step]],
    "GR07": [round(float(x), 4) for x in a[::step]],
    "GR06": [round(float(x), 4) for x in b[::step]],
}

# GR07 period histogram from a 1 s single-channel capture at 250 MS/s: the
# clock-retiming staircase.
try:
    import pandas as pd
    df = pd.read_parquet(CAP)
    vals, counts = np.unique(np.round(df["observable_ns"].to_numpy(), 1), return_counts=True)
    order = np.argsort(-counts)[:8]
    hist = sorted([[float(vals[i]), int(counts[i])] for i in order])
    # The same capture answers a second question: is the quantisation noise
    # shaped, or white? The deck claimed first-order shaping; keeping every
    # edge is the only way to test it, and it is not.
    from scipy import signal as _sig
    per = df["observable_s"].to_numpy() if "observable_s" in df else \
        df["observable_ns"].to_numpy() * 1e-9
    _fs = 1.0 / per.mean()
    _x = per / per.mean() - 1.0
    _f, _p = _sig.welch(_x, fs=_fs, nperseg=16384, detrend="constant")
    _band = _f > 1
    _cyc = per.mean() * 64e6
    _q = _cyc % 1
    res["staircase"] = {
        "total": int(len(df)),
        "distinct": int(vals.size),
        "bins": hist,
        "mean_ns": float(df["observable_ns"].mean()),
        "hf": {
            "fs_hz": float(_fs),
            "nyquist_hz": float(_fs / 2),
            "board_nyquist_hz": 500.0,
            "flat_variation": float(_p[_band].max() / _p[_band].min()),
            "integral_ppm": float(np.sqrt(np.trapezoid(_p, _f)) * 1e6),
            "sigma_ppm": float(_x.std(ddof=1) * 1e6),
            "dither_pred_ppm": float(np.sqrt(_q * (1 - _q))
                                     * ((1 / 64e6) / per.mean()) * 1e6),
            "q": float(_q),
        },
    }
except Exception as exc:
    res["staircase"] = {"error": f"{type(exc).__name__}: {exc}"}

json.dump(res, open(OUT, "w"), indent=1)

s = res["sensors"]
print(f"readings={res['n_readings']} span={res['span_s']/60:.1f} min")
for k in ("GR07", "GR06"):
    v = s[k]
    print(f"{k}: mean={v['mean_c']:.4f} C  reading_std={v['reading_std_c']*1000:.1f} mK  "
          f"p2p={v['peak_to_peak_c']*1000:.0f} mK  sem={v['sem_c']*1000:.2f} mK  "
          f"events={v['events_per_capture']:.0f}  rejected={v['rejected_total']:.0f}")
    for w in ("short", "long"):
        al = v["allan"][w]
        if al:
            print(f"      allan @ {al['tau_s']:6.1f} s = {al['dev_c']*1000:6.2f} mK")
print(f"correlation GR07 vs GR06 = {res['correlation']:.4f}")
print(f"offset (GR06-GR07) = {res['mean_offset_c']*1000:.1f} mK, diff std = {res['diff_std_c']*1000:.1f} mK")
if "bins" in res.get("staircase", {}):
    st = res["staircase"]
    print(f"staircase: {st['distinct']} distinct values in {st['total']} periods; "
          f"top bins: {[(v, c) for v, c in st['bins']]}")
print("wrote", OUT)
