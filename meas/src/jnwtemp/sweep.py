#!/usr/bin/env python3
######################################################################
##        Copyright (c) 2026 Carsten Wulff Software, Norway
## ###################################################################
##  The MIT License (MIT)
##
##  Permission is hereby granted, free of charge, to any person obtaining a copy
##  of this software and associated documentation files (the "Software"), to deal
##  in the Software without restriction, including without limitation the rights
##  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
##  copies of the Software, and to permit persons to whom the Software is
##  furnished to do so, subject to the following conditions:
##
##  The above copyright notice and this permission notice shall be included in all
##  copies or substantial portions of the Software.
######################################################################
"""Step the chamber through setpoints, logging both sensors at each plateau.

Board only - no Saleae. The RP2350's PIO times GR07 and GR06 and generates the
ResetTemp06 train itself, so a sweep needs nothing but the serial port and the
chamber's network connection.

Each point is commanded, then left to *stabilise* (inside ``tol_c`` of the
setpoint continuously for ``soak_s``), then measured for ``dwell_s``. Rows are
written while settling too, tagged in the ``phase`` column, so only
``phase == 'dwell'`` rows belong in a calibration fit.

Blocking and Qt-free, unlike :class:`~jnwtemp.chamber_worker.ChamberThread`
which drives the same three phases as a state machine for the GUI. This one is
meant to be run unattended and read start to finish before it is trusted with
an overnight run.

Two files are written into the output folder: ``sweep.csv`` and
``sweep.meta.json``. The sidecar holds the whole :class:`SweepConfig`, so a
field added to it is recorded without anything else having to change, and it is
written before the run as well as after, so a run that is killed still says how
it was configured.
"""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

from . import __version__
from .acquire import BOTH, SENSORS, AcquireSettings
from .board import TTBoard
from .boardmeas import BoardMeasurement
from .chamber import (
    DEFAULT_ADDRESS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ChamberError,
    VotschChamber,
)
from .chamber_worker import SweepPlan
from .recorder import _git_describe
from .temperature import CalibrationStore

#: Sensors measured, in column order. Both come out of one PIO window.
KEYS: List[str] = list(SENSORS)


@dataclass
class SweepConfig:
    """Everything that shapes a sweep; serialised whole into the sidecar."""

    start_c: float = 20.0
    stop_c: float = 30.0
    step_c: float = 5.0
    #: stabilisation band around the setpoint
    tol_c: float = 0.3
    #: time inside the band before the plateau starts
    soak_s: float = 120.0
    #: length of the measurement plateau
    dwell_s: float = 300.0
    #: give up on a point that will not stabilise
    max_settle_s: float = 1800.0
    #: seconds of PIO counting per reading
    window_s: float = 1.0
    #: sub-window bins, which is where the per-reading sigma comes from
    bin_ms: float = 1.0
    #: target time between readings
    period_s: float = 5.0
    #: leave the chamber running at the last setpoint instead of switching off
    leave_on: bool = False
    clock_hz: int = 64_000_000
    reset_high_us: int = 20
    reset_low_us: int = 200
    chamber_host: str = DEFAULT_HOST
    chamber_port: int = DEFAULT_PORT
    chamber_address: int = DEFAULT_ADDRESS
    board_port: Optional[str] = None

    def setpoints(self) -> List[float]:
        return SweepPlan(self.start_c, self.stop_c, self.step_c).setpoints()

    def acquire_settings(self) -> AcquireSettings:
        return AcquireSettings(
            sensor=BOTH,
            window_s=self.window_s,
            bin_ms=self.bin_ms,
            clock_hz=self.clock_hz,
            reset_high_us=self.reset_high_us,
            reset_low_us=self.reset_low_us,
        )


def columns() -> List[str]:
    cols = ["t_s", "t_unix", "phase", "set_c", "chamber_c"]
    for k in KEYS:
        cols += [f"{k}_temp_c", f"{k}_sem_c", f"{k}_sigma_c",
                 f"{k}_rate_hz", f"{k}_observable_ns", f"{k}_n", f"{k}_bins"]
    return cols


def _row(t0: float, phase: str, target: float, actual_c: float,
         readings: Dict[str, object]) -> list:
    row = [f"{time.time() - t0:.3f}", f"{time.time():.3f}", phase,
           f"{target:g}", f"{actual_c:.2f}"]
    for k in KEYS:
        r = readings.get(k)
        if r is None or not r.ok:
            row += ["", "", "", "", "", 0, 0]
            continue
        sem = r.std_temp_c / max(1.0, r.n ** 0.5)
        row += [f"{r.mean_temp_c:.6f}", f"{sem:.6f}", f"{r.std_temp_c:.6f}",
                f"{r.mean_rate_hz:.6f}", f"{r.mean_s * 1e9:.6f}", r.n,
                int(r.bin_rate_hz.size)]
    return row


def default_out_dir() -> str:
    return os.path.join("data", time.strftime("sweep-%Y%m%d-%H%M%S"))


def run_sweep(
    cfg: SweepConfig,
    out_dir: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> str:
    """Run the sweep and return the folder it wrote. Never raises on a bad read."""
    out_dir = os.path.abspath(out_dir or default_out_dir())
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "sweep.csv")
    meta_path = os.path.join(out_dir, "sweep.meta.json")

    t0 = time.time()
    meta = {
        "format": "jnwtemp-sweep/1",
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
        "config": asdict(cfg),
        "setpoints_c": cfg.setpoints(),
        "sensors": KEYS,
        "software": {"jnwtemp": __version__, "git": _git_describe()},
    }

    def save_meta():
        with open(meta_path, "w") as fh:
            json.dump(meta, fh, indent=2)

    save_meta()

    board = TTBoard(port=cfg.board_port)
    info = board.connect()
    log(f"board  : {info.banner}")
    if "jnw_wulffern" not in info.project:
        log(f"select : {board.select_project()}")
    log(f"clock  : {board.set_clock_hz(cfg.clock_hz)} Hz")
    meas = BoardMeasurement(board, log=lambda m: log(f"         {m}"))
    log(f"pio    : {meas.setup()}")

    store = CalibrationStore()
    cals = {k: store.get(k) for k in KEYS}
    acq = cfg.acquire_settings()

    chamber = VotschChamber(cfg.chamber_host, cfg.chamber_port, cfg.chamber_address)
    log(f"chamber: {chamber.connect().describe()}")

    fh = open(csv_path, "w", newline="")
    writer = csv.writer(fh)
    writer.writerow(columns())
    rows = 0

    def measure(phase: str, target: float, actual_c: float) -> None:
        """One PIO window of both sensors, appended as a row."""
        nonlocal rows
        try:
            readings = meas.read(acq, cals)
        except Exception as exc:  # noqa: BLE001 - one bad window must not end a run
            log(f"    read failed: {type(exc).__name__}: {str(exc)[:90]}")
            return
        writer.writerow(_row(t0, phase, target, actual_c, readings))
        fh.flush()
        rows += 1

    setpoints = cfg.setpoints()
    log(">>> " + " → ".join(f"{p:g}" for p in setpoints) + f" °C into {out_dir}")
    try:
        for i, target in enumerate(setpoints, 1):
            log(f"--- point {i}/{len(setpoints)}: {target:g} °C")
            chamber.set_temp(target, on=True)
            stable, settled = None, False
            deadline = time.time() + cfg.max_settle_s
            while time.time() < deadline:
                st = chamber.status()
                if not st.running:  # it has been seen to stop on its own
                    log("    chamber stopped by itself - restarting")
                    chamber.set_temp(target, on=True)
                    st = chamber.status()
                if abs(st.actual_c - target) <= cfg.tol_c:
                    stable = stable or time.time()
                    if time.time() - stable >= cfg.soak_s:
                        settled = True
                        break
                else:
                    stable = None
                log(f"    settling {st.actual_c:6.2f} °C")
                measure("settling", target, st.actual_c)
                time.sleep(max(0.0, cfg.period_s - cfg.window_s))
            if not settled:
                log(f"    NOT STABLE within {cfg.max_settle_s:g}s - skipped")
                continue
            log(f"    dwelling {cfg.dwell_s:g}s")
            end = time.time() + cfg.dwell_s
            while time.time() < end:
                measure("dwell", target, chamber.status().actual_c)
                time.sleep(max(0.0, cfg.period_s - cfg.window_s))
    except KeyboardInterrupt:
        meta["interrupted"] = True
        log("interrupted")
    except ChamberError as exc:
        meta["error"] = str(exc)
        log(f"chamber error: {exc}")
    finally:
        fh.close()
        meas.close()
        board.disconnect()
        try:
            final = chamber.status() if cfg.leave_on else chamber.stop()
            log(f"chamber: {final.describe()}")
        except ChamberError as exc:
            log(f"chamber: could not switch off: {exc}")
        chamber.close()
        meta["result"] = {
            "rows": rows,
            "duration_s": time.time() - t0,
            "csv": os.path.basename(csv_path),
        }
        save_meta()
        log(f">>> {rows} rows -> {csv_path}")
    return out_dir
