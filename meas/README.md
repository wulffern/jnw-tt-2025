# jnwtemp - live readout for JNW-TEMP (Tiny Tapeout project 258)

A PySide6 + pyqtgraph interface that reads the two NTNU student temperature
sensors on `tt_um_jnw_wulffern` in real time: it plots the sensor period, the
estimated temperature, and a long-running noise spectrum.

It closes the loop over both instruments:

* **Saleae Logic Pro** via the Logic 2 automation API - repeated short captures
  at the highest rate the device will grant, parsed as edge transitions rather
  than samples. The legal rates depend on how many channels are enabled and are
  negotiated with the device, not assumed: the Logic Pro 16 here tops out at
  **250 MS/s** (4 ns) on one channel and 25 MS/s on eight.
* **Tiny Tapeout demo board** via the RP2350's MicroPython REPL - selects
  project 258, sets the project clock, and generates the GR06 reset burst.

## Running

```sh
cd meas
python3 main.py              # or: pip install -e . && jnwtemp
jnwtemp --no-board           # Saleae only, board driven by hand
jnwtemp --port /dev/cu.usbmodem212301
```

### Prerequisites

1. **Logic 2 automation server.** Logic 2 -> Preferences -> Automation ->
   *Enable automation server* (port 10430). Without it the app cannot capture;
   nothing else will make it work.
2. **A free serial port.** macOS reports the port busy while a Chrome tab holds
   it over WebSerial - close TT Commander before connecting.
3. `pip install -r requirements.txt` (or `pip install -e .`).

## The two sensors

From `info.yaml` and the "How to test" section of `docs/info.md`:

| Sensor | Output | Stimulus | Observable |
| :--- | :--- | :--- | :--- |
| GR07 | `uo_out[0]` (Pwm07) | none, free-running; run the clock fast | period |
| GR06 | `uo_out[2]` (Pwm06) | `ui_in[0]` (ResetTemp06) high then low | high pulse width |

Both are a PTAT current charging a capacitor into a comparator, so the time they
produce is

    t = V_ref * C / I(T),   I(T) = k*T*ln(N) / (q*R)

which is inversely proportional to absolute temperature. The app therefore works
in the **rate** domain, `r = 1/t`, which is proportional to absolute temperature,
and fits `r = a*T_K + b`.

### Which Saleae channel?

The channel numbers in the GUI are whatever your probe clips land on, not fixed.
Press **Detect active channels** - it captures D0-D7 briefly and reports edge
count, frequency and duty per channel, so the wiring is measured rather than
guessed.

## Calibration

There is no absolute reference on the chip, so temperature needs at least one
known point:

1. Let the reading settle, set **Reference** to the true ambient (e.g. 23 °C).
2. Press **Calibrate here**.

One point fits the ideal PTAT model through the origin (`b = 0`), which is the
only honest choice from a single observation. Add a second point at a different
temperature and it upgrades to a full affine fit, which absorbs comparator delay
and reset time. Calibrations are stored per sensor in
`~/.config/jnwtemp/calibration.json`.

## Capture length

Longer captures average down cleanly - the noise is white over at least a
second, so the standard error follows 1/sqrt(N) with no drift floor inside a
capture. Measured on GR07 at 250 MS/s:

| Requested | Actual | Wall clock | Edges | Export | Periods | SEM on the period |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.10 s | 0.100 s | 0.94 s | 182 k | 1.5 MB | 91 k | 26.6 ps |
| 0.25 s | 0.267 s | 1.02 s | 486 k | 3.9 MB | 243 k | 16.3 ps |
| 0.50 s | 0.501 s | 1.21 s | 911 k | 7.3 MB | 455 k | 11.9 ps |
| 1.00 s | 1.003 s | 1.62 s | 1.8 M | 14.6 MB | 911 k | 8.4 ps |
| 2.00 s | 2.005 s | 2.81 s | 3.6 M | 29.1 MB | 1.8 M | 6.0 ps |

Wall clock is roughly `0.8 s + duration`, so a 1 s capture costs ~1.7 s per
reading and yields **~2 mK** on the mean (8.4 ps on 1100 ns). Requests below
~0.2 s are rounded up by Logic 2; from 0.5 s upward the duration is honoured
exactly. The **Capture** control accepts up to 5 s.

Note that 2 mK is the *statistical* precision of one reading, not the accuracy:
successive 1 s readings drift over ~0.3 K in tens of seconds, which is real
thermal behaviour and two orders of magnitude larger. Use the Allan plot to see
where averaging stops helping.

## Measuring both sensors at once

The **Both - GR07 + GR06 together** option captures two channels in one go, so
the sensors share a time base and a thermal environment - which is what makes
comparing them mean anything. One **Calibrate here** press calibrates both from
the same capture, since on a die this small they are at the same temperature.

The temperature plot then draws both traces, the second and third hero tiles
become GR06's temperature and the GR06 − GR07 difference, and the raw-timing and
spectrum panes follow GR07. Two channels caps the Logic Pro at 125 MS/s (8 ns),
still finer than GR07's 15.6 ns clock quantum.

Watch the bin readout: at 1 ms GR07 gets ~910 events per point but GR06 only ~4,
so it warns *"GR06 too thin, widen the bin"*. ~20 ms suits GR06.

## Capture rate, and why it is what it is

Each capture costs a fixed ~0.7 s on top of its own length. Measured breakdown
per capture at 250 MS/s:

| stage | time | what it is |
| :--- | ---: | :--- |
| arm | 0.21 s | `start_capture` round trip |
| trigger latency | ~0.30 s | `wait()` beyond the requested duration |
| export | 0.09 s | `export_raw_data_binary`, flat with size |
| parse | 0.003 s | reading the transitions - effectively free |
| close | 0.10 s | releasing the capture in Logic 2 |

None of it is ours to optimise: parsing is already 0.3 % of the total and the
rest is Logic 2 round trips. The lever is therefore capture *length*, because
the overhead is fixed:

| capture | wall clock | duty | 1 ms points per second |
| ---: | ---: | ---: | ---: |
| 0.05 s | 0.90 s | 6 % | 56 |
| 0.20 s | 0.90 s | 22 % | 222 |
| 0.50 s | 1.21 s | 41 % | 413 |
| 1.00 s | 1.71 s | 58 % | 585 |
| 2.00 s | 2.81 s | 71 % | 712 |

So a longer capture is strictly better for throughput and for gap-free traces;
it costs only how often the display updates. Requests below ~0.2 s are pointless
- Logic 2 rounds them up and the duty cycle collapses. The panel shows the
measured duty next to the bin size.

## Recording

**Record to CSV...** streams one row per capture straight to disk, flushed every
row, so a run that goes overnight survives the app dying and the file can be
plotted while it is still growing. **Stop recording** closes it and reports the
row count. A red `● REC` badge sits on the temperature plot while it runs.

Recordings run at the trace-bin resolution - the same 1 ms points the plot
shows, ~1.4 MB/min. Per-event recording was measured and rejected: GR07 emits
~910k periods a second, which is 77 GB/hour as CSV and still 11 GB/hour as
Parquet. A single capture's per-event detail is available on demand through
**Export last capture**. In dual mode the two sensors share bin edges, so they
are written as aligned wide columns (`GR07_temp_c`, `GR06_temp_c`, ...) rather
than a long format that would need pivoting to plot.

Each recording writes **two** files: `run.csv` and a `run.meta.json` sidecar.
The sidecar is the machine-readable provenance - when and by whom (local and UTC
timestamps, timezone, user, host), the software (jnwtemp and Python versions,
platform, git commit and whether the tree was dirty), the DUT (project, shuttle
index, sensor, pins, clock), the acquisition settings, the instrument identities
reported at connect, and the full calibration including every calibration point.
On stop it is rewritten with the row count and duration, so it describes the
finished run rather than only its intent. The CSV repeats a readable summary in
`#` header lines so it still stands alone if the sidecar is lost - a column of
degrees cannot be re-interpreted later without the model that produced it.
Alongside `temp_c` the raw `rate_hz` and `observable_s` are recorded, so a run
taken under a one-point calibration can be re-derived later against a better
one. Recording starts the acquisition loop if it is paused - otherwise it would
silently write nothing.

`Save CSV...` is the separate, simpler action: it dumps whatever is currently in
the in-memory history.

**Export last capture...** writes the *per-event* data of the most recent
capture - every individual period or pulse, with its time, rate and temperature
- plus its own `.meta.json`. This is the raw view that shows GR07's retiming
staircase. Format follows the extension: `.csv`, `.parquet` or `.feather`. Use a
binary format for long captures; a 1 s capture is 911k events, which is 68 MB as
CSV but 9.8 MB as Parquet.

## Plotting in cicwave

Both output files load in [cicwave](https://github.com/wulffern/cicwave) with no
flags, including through its MCP `plot` tool:

```sh
cicwave run.csv --x t_rel_s          # temperature vs time
cicwave capture.parquet --x t_s      # every period in one capture
```

Two deliberate choices make that work:

* **No `#` comment banner in the data files.** cicwave only strips comment lines
  when told to with `--csv-comment '#'`, and its MCP `plot` tool has no way to
  pass that - a commented CSV fails to parse with a `ParserError`. So the data
  files are a plain header row plus numbers, and all provenance lives in the
  JSON sidecar beside them.
* **Unit-suffixed column names** (`t_rel_s`, `observable_s`, `observable_ns`,
  `rate_hz`, `temp_c`) so cicwave's engineering-unit detection labels the axes
  itself - it renders `observable_ns` values around 1100 as "1.1 µs".

## Reading the plots

* **Estimated temperature** - the temperature-vs-time trace, with the mean and
  ±1σ band. Each capture is re-reduced into **trace bins** (1 ms by default), so
  a 0.5 s capture contributes ~500 points rather than one average, and the
  millisecond-scale noise is visible instead of being smoothed away. The line
  breaks between captures: the arming and export time is real dead time, and
  drawing across it would invent data.

  Smaller bins mean fewer events per point and so a noisier point; the panel
  reports both, e.g. *"569 pts/capture at 1.00 ms, 910 events each, ≈ 69 mK
  noise per point"*, and warns when a bin holds fewer than ten events. GR07 at
  ~910 kHz suits 1 ms; GR06 only fires ~4500 times a second, so it needs ~20 ms
  bins to get a comparable number of events per point. Set the bin to 0 to go
  back to one point per capture.

  Note the trace is deliberately **not** what feeds the spectra: it is sampled
  in bursts with gaps, which would violate the uniform-sampling assumption in
  the PSD and Allan routines. Those use the per-capture averages instead.
* **Last capture - raw timing** - every individual period or pulse in the most
  recent capture. For GR07 expect a *staircase*: the comparator output is
  re-timed by the project clock, so one clock period is roughly 4-5 K at 64 MHz.
  Averaging tens of thousands of events dithers through it; that is why the
  clock is run as fast as it will go.
* **Bottom plot** - selectable:
  * *Within capture* - conversion noise of the sensor at its own ~1 MHz rate.
  * *Long term* - drift and 1/f across the whole session.
  * *Allan deviation* - how far averaging actually helps before drift takes over.

## Comparing the two sensors

`scripts/dual_log.py` logs **both** sensors at once. One capture covers D0 (GR07)
and D2 (GR06) while the ResetTemp06 burst runs on the RP2350, so the two share a
time base and thermal environment — which is what makes the comparison mean
anything. Two channels caps the Logic Pro at 125 MS/s (8 ns), still finer than
GR07's 15.6 ns clock quantum, and GR06's 14.3 ns jitter dithers over it.

```sh
python3 scripts/dual_log.py 900        # 15 min -> data/dual.csv
python3 scripts/analyse_dual.py        # -> data/deck_data.json + a summary
python3 scripts/build_presentation.py  # -> docs/presentation-built.html
```

`data/2026-07-31_dual_15min.csv` is one such run, 569 readings over 15 minutes.
What it shows:

| Per 0.5 s reading | GR07 | GR06 |
| :--- | ---: | ---: |
| Mean | 23.028 °C | 22.960 °C |
| Statistical precision (SEM) | **3.31 mK** | 12.82 mK |
| Reading-to-reading spread | 172.8 mK | 104.0 mK |
| Peak-to-peak over the run | 1041 mK | 738 mK |
| Best Allan deviation | 57 mK at τ = 8 s | **31 mK at τ = 76 s** |

Two results worth keeping in mind when using either sensor:

* **They do not track each other.** Correlation is +0.11, and the spread of their
  difference (192 mK) is 95 % of the quadrature sum of the individual spreads
  (202 mK) — the signature of largely independent noise. On a die this small the
  two are at the same temperature, so the ~1 K of wander is measuring the
  *sensors*, not the room.
* **The Allan curves cross.** GR07 bottoms out at 57 mK after 8 s of averaging
  and degrades from there; GR06 keeps improving to 31 mK at 76 s. GR07's ~200×
  event advantage buys a better single reading, not a better long-term
  measurement. Past a few seconds GR06 is the more trustworthy of the two.

### Stimulus runs

`data/jnwtemp-BOTH-20260731-213248.csv` is a three-minute dual-sensor run with
freeze spray at 12.9 s and a fingertip on the package from 88 s to 122 s.

```sh
python3 scripts/analyse_events.py data/<run>.csv   # find and rate the events
python3 scripts/build_event_data.py                # -> data/event_data.json
```

What it showed:

* The spray took the die from **21.6 °C to 8.2 °C in 1.41 s** - 13.4 K, peaking
  at **-37 K/s**. A fingertip manages about +11 K/s on and -8.5 K/s off.
* **The two sensors agree almost perfectly when something real happens**
  (r = +0.95 during the spray, +0.99 under the finger) and disagree when nothing
  does (+0.11 over a quiet 15 minutes). That settles the question the quiet run
  raised: the wander there was sensor noise, not the room.
* **GR07 has dead zones.** Its noise per 1 ms point ranges from 8.8 mK to 90.7 mK
  depending on temperature, and collapses exactly where the period lands on a
  whole number of 64 MHz clock cycles. That is the dither switching off, not
  precision - the sensor goes locally insensitive while looking ten times
  quieter, which is also why its Allan curve turns up.

Two things to keep in mind reading those numbers. Single-point calibration means
the *depth* is extrapolated 15 K beyond its only anchor and carries an unbounded
systematic; the *rates* depend only on the slope and are far more trustworthy.
And a peak rate is a bandwidth statement: GR07 resolves the spray with a 46 ms
window, GR06 needs 406 ms and so reports -17 K/s for the same event.

Differentiating a burst-sampled trace has two traps, both handled in
`analyse_events.py`: never differentiate across the dead time between captures
(hence the `capture` column), and drop the half-window at each capture edge,
where a Savitzky-Golay filter extrapolates and will happily report tens of K/s
across a perfectly flat boundary.

### The slides

`docs/presentation.html` is a standalone deck covering the sensors, the setup and
these results. It is published with the project's GitHub Pages site:

<https://analogicus.com/jnw-tt-2025/presentation.html>

(The `github.io` address redirects there — the site uses a custom domain.)

The site root stays what Tiny Tapeout's action makes it - a redirect to the 3D
GDS viewer. The `viewer` job in `.github/workflows/gds.yaml` gained two steps
that stage the deck into `gh-pages/` *before* `tt-gds-action/viewer` runs: that
action fills the directory with `download-artifact` (which merges into an
existing directory rather than replacing it) and then uploads the whole thing,
so anything staged first gets published alongside the viewer. Nothing about the
TT action is forked or overridden, and the copy is guarded by a file-exists test
so a missing deck can never fail the Pages deploy.

Editing: `docs/presentation.template.html` holds the layout with a `__DATA__`
placeholder; `build_presentation.py` fills it from `data/deck_data.json` to
produce the committed `docs/presentation.html`. No number in the deck is typed by
hand, so it cannot drift from the measurements. Re-run all three scripts after a
new capture.

## The plot window

If [cicwave](https://github.com/wulffern/cicwave) is importable, the temperature
pane *is* cicwave's `PgWavePlot`, so its measurement ergonomics come along:
A/B cursors with a delta readout, min/max/mean/σ/rms/peak-to-peak per series, and
the usual keys — **z** zoom in, **Z** zoom out, **a**/**b** cursors, **f** fit,
**l** legend. Without cicwave the app falls back to its own plot and the rest is
unchanged.

The bridge is deliberately small (`cicwave_plot.py`): cicwave's `WaveFile`
accepts an in-memory DataFrame and exposes a `df` setter, and `PgWave.reload()`
re-reads its column from it, so swapping the frame each capture updates the
curves in place — cursors, zoom and legend survive. Auto-fit stops the moment you
zoom, because a plot that keeps re-framing itself is useless for reading values
off; **f** hands it back.

## Layout

| File | Role |
| :--- | :--- |
| `edges.py` | Saleae binary export parser, period/pulse-width extraction |
| `logic.py` | Logic 2 automation: connect, configure, capture, channel scan |
| `board.py` | MicroPython REPL control of the demo board |
| `temperature.py` | Rate-to-temperature model, calibration store |
| `spectrum.py` | Welch PSD, Allan deviation, bounded history |
| `acquire.py` | The measurement loop; sensor definitions |
| `worker.py` | Qt thread wrapping `acquire` |
| `recorder.py` | Streaming CSV recorder |
| `scripts/dual_log.py` | Log both sensors simultaneously |
| `scripts/analyse_dual.py` | Compare the two, emit deck data |
| `scripts/build_presentation.py` | Inject measured data into the deck |
| `plots.py` / `main_window.py` | The GUI |
| `cicwave_plot.py` | Live trace via cicwave's waveform plot (optional) |

The control panel keeps the everyday controls visible - sensor, capture length,
trace bin, calibrate, record - and folds wiring and one-time configuration into
a collapsed **Setup / wiring** section: the sample rate is negotiated with the
device, the threshold suits both logic levels and the clock always runs flat
out, so those cost panel height for settings nobody touches twice. The panel
scrolls, so it never dictates a minimum window height.

## Measured behaviour (room temperature, 64 MHz project clock)

Both sensors read out through D0-D2 wired straight to `uo_out[0..2]`:

| | GR07 | GR06 |
| :--- | :--- | :--- |
| Observable | period **1099 ns** (909.8 kHz) | pulse width **7078 ns** |
| High pulse | 13.7 ns | (the observable) |
| Per-event spread | 7.7 ns, on a 2-level staircase | 14.3 ns, continuous |
| Events per capture | ~180 000 | ~700 |
| Resulting noise | ~5 mK | ~23 mK |

Note what this means for outlier rejection: a spread-based (MAD/sigma) rule
reads the far cluster as outliers and deletes it, destroying the dither and
biasing the mean by a whole clock period (~4 K). Rejection is therefore by
*ratio* to the median (default ±40%), which catches the thing that actually goes
wrong - a missed or spurious edge, which doubles or halves the interval - and is
blind to any amount of legitimate dither.

GR07's periods take only **four distinct values** - 1092/1096 and 1108/1112 ns.
The 16 ns gap between the two clusters is one 64 MHz clock period (15.6 ns):
the comparator output is re-timed by the project clock, so each period is either
N or N+1 clock cycles. The 4 ns substructure is the Saleae's own 250 MS/s
quantum. Resolution comes entirely from the *dither ratio* between the two
levels, which is why the reading is averaged over ~180k periods and why the
clock is run as fast as it goes. GR06 is asynchronous and shows no such
staircase, but yields ~250x fewer events per capture.

## Notes on the hardware

* The board is a **TinyTapeout RP2350B Core**. Its USB CDC only answers once
  DTR/RTS are asserted after opening the port.
* Max project clock is **64 MHz**: the SDK caps the system clock at 128 MHz and
  the PWM divider cannot go below 2.
* Writing `tt.ui_in[0]` through the SDK's `Logic` wrapper costs ~15 ms per edge.
  `tt.pins.ui_in0.raw_pin.value()` costs ~6 µs, so the reset burst uses the raw
  pin and runs on the board rather than as host round-trips.
