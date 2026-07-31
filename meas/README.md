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

## Recording

**Record to CSV...** streams one row per capture straight to disk, flushed every
row, so a run that goes overnight survives the app dying and the file can be
plotted while it is still growing. **Stop recording** closes it and reports the
row count. A red `● REC` badge sits on the temperature plot while it runs.

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

* **Estimated temperature** - the temperature-vs-time trace, one averaged point
  per capture, with the mean and ±1σ band.
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
