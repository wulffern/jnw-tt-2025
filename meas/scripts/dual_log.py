"""Log BOTH JNW-TEMP sensors simultaneously for a while.

One capture covers D0 (GR07, free-running) and D2 (GR06, reset-triggered) at
once, with the ResetTemp06 burst running on the RP2350 during the capture. That
gives the two sensors an identical time base and thermal environment, which is
what makes the comparison meaningful.

At 2 channels the Logic Pro allows 125 MS/s (8 ns). GR07 is unaffected - its
period is quantized by the 64 MHz project clock (15.6 ns), which is coarser -
and GR06's 14.3 ns jitter still dithers well over an 8 ns quantum.
"""
import csv, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from jnwtemp.acquire import SENSORS, AcquireSettings, reduce_train
from jnwtemp.board import TTBoard
from jnwtemp.logic import CaptureSettings, LogicCapture
from jnwtemp.temperature import Calibration

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
os.makedirs(OUT, exist_ok=True)
CSV = os.path.join(OUT, "dual.csv")

RUN_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 720.0
CAPTURE_S = 0.5
REF_C = 23.0
CH = {"GR07": 0, "GR06": 2}
RESET_HIGH_US, RESET_LOW_US = 20, 200

st = AcquireSettings(sample_rate=125_000_000, threshold_volts=1.2, duration_s=CAPTURE_S,
                     reset_high_us=RESET_HIGH_US, reset_low_us=RESET_LOW_US)

board = TTBoard()
info = board.connect()
print("board:", info.banner, flush=True)
print("clock:", board.set_clock_hz(64_000_000), flush=True)
board.set_ui_in(0, 0)

lc = LogicCapture(CaptureSettings(channels=[0, 2], sample_rate=125_000_000,
                                  threshold_volts=1.2, duration_s=CAPTURE_S))
print("logic:", lc.connect(), flush=True)

cals = {k: Calibration(k) for k in CH}
n_pulses = max(1, int((CAPTURE_S + 1.0) * 1e6 /
                      (RESET_HIGH_US + RESET_LOW_US + TTBoard.PULSE_LOOP_OVERHEAD_US)))


def one_round():
    """Stimulate GR06 and capture both channels concurrently."""
    board.pulse_ui_in_begin(0, n_pulses, RESET_HIGH_US, RESET_LOW_US)
    try:
        trains = lc.capture()
    finally:
        board.pulse_ui_in_end(n_pulses, RESET_HIGH_US, RESET_LOW_US)
    out = {}
    for key, ch in CH.items():
        out[key] = reduce_train(trains[ch], SENSORS[key], cals[key], band=st.outlier_band)
    return out


print(">>> warm-up + one-point calibration at %.2f C" % REF_C, flush=True)
r = one_round()
for k in CH:
    cals[k].add_point(REF_C, r[k].mean_rate_hz)
    print(f"    {k}: {r[k].n:>7} events, {r[k].mean_s*1e9:10.3f} ns -> {cals[k].describe()}",
          flush=True)

cols = ["t_s"]
for k in ("GR07", "GR06"):
    cols += [f"{k}_temp_c", f"{k}_sem_c", f"{k}_sigma_c",
             f"{k}_rate_hz", f"{k}_observable_ns", f"{k}_n", f"{k}_rejected"]

fh = open(CSV, "w", newline="")
w = csv.writer(fh)
w.writerow(cols)
t0 = time.time()
i = 0
print(f">>> logging both sensors for {RUN_SECONDS/60:.1f} min -> {CSV}", flush=True)
while time.time() - t0 < RUN_SECONDS:
    try:
        r = one_round()
    except Exception as exc:
        print(f"    round failed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        time.sleep(1.0)
        continue
    t = time.time() - t0
    row = [f"{t:.4f}"]
    for k in ("GR07", "GR06"):
        x = r[k]
        sem = x.std_temp_c / max(1.0, x.n ** 0.5)
        row += [f"{x.mean_temp_c:.6f}", f"{sem:.6f}", f"{x.std_temp_c:.6f}",
                f"{x.mean_rate_hz:.6f}", f"{x.mean_s*1e9:.6f}", x.n, x.n_rejected]
    w.writerow(row)
    fh.flush()
    i += 1
    if i % 20 == 0 or i < 3:
        print(f"    [{i:4d}] t={t:7.1f}s  GR07={r['GR07'].mean_temp_c:8.4f} C "
              f"(n={r['GR07'].n:>7})  GR06={r['GR06'].mean_temp_c:8.4f} C "
              f"(n={r['GR06'].n:>5})", flush=True)

fh.close()
lc.close()
board.set_ui_in(0, 0)
board.disconnect()
print(f">>> wrote {i} rows to {CSV}", flush=True)
print("DONE", flush=True)
