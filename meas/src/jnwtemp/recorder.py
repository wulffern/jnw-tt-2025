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
"""Streaming CSV recorder for a temperature run.

Each reading is appended and flushed as it arrives rather than being held in
memory and written at the end, so a recording that runs overnight survives the
app being killed, and the file can be tailed or plotted while it grows.

The header carries the calibration and instrument settings in ``#`` comment
lines, because a column of degrees is not interpretable later without the model
that produced it.
"""

from __future__ import annotations

import csv
import getpass
import json
import os
import platform
import socket
import subprocess
import sys
import time
from typing import List, Optional, TextIO

from . import __version__
from .acquire import SENSORS, AcquireSettings, Reading
from .temperature import Calibration

#: One row per *trace bin* - the same time resolution the live plot shows,
#: 1 ms by default. Per-event recording was measured and rejected: GR07 emits
#: ~910k periods a second, which is 77 GB/hour as CSV and still 11 GB/hour as
#: Parquet. A 1 ms bin is 1.1 MB/min, and a whole capture's per-event detail is
#: still available on demand through export_capture().
COLUMNS = [
    "t_rel_s",       # bin centre, seconds since the recording started
    "t_unix",        # absolute time, so runs correlate with anything else
    "temp_c",        # temperature for this bin
    "temp_sem_c",    # its standard error, so points carry their own error bar
    "rate_hz",       # the raw observable, in case the model changes later
    "observable_ns",
    "events",        # events averaged into this bin
    "capture",       # index of the capture the bin came from
]


def _git_describe() -> Optional[dict]:
    """Commit and dirty-state of the repo this package lives in, if it is one."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", here, *args],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()

        return {
            "commit": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain")),
        }
    except (OSError, subprocess.SubprocessError):
        return None


def build_provenance(
    settings: AcquireSettings,
    cal: Calibration,
    t0: float,
    instruments: Optional[dict] = None,
    sweep: Optional[dict] = None,
) -> dict:
    """Everything needed to interpret or reproduce a recording later.

    Kept as a separate dict so it can be written to a JSON sidecar as well as
    summarised into the CSV header.
    """
    spec = SENSORS.get(settings.sensor)
    return {
        "format": "jnwtemp-recording/1",
        "created": {
            "iso8601": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))
            + time.strftime("%z", time.localtime(t0)),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
            "unix": t0,
            "timezone": time.strftime("%Z", time.localtime(t0)),
        },
        "operator": {
            "user": _safe(getpass.getuser),
            "host": _safe(socket.gethostname),
        },
        "software": {
            "jnwtemp": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git": _git_describe(),
        },
        "dut": {
            "project": "tt_um_jnw_wulffern",
            "shuttle_index": 258,
            "shuttle": "ttsky25a",
            "sensor": settings.sensor,
            "sensor_description": spec.doc if spec else "",
            "output_pin": f"uo_out[{spec.uo_bit}]" if spec else "",
            "stimulus_pin": (
                f"ui_in[{spec.ui_bit}]" if spec and spec.ui_bit is not None else None
            ),
            "project_clock_hz": settings.clock_hz,
        },
        "acquisition": {
            "saleae_channel": settings.channel,
            "requested_sample_rate_hz": settings.sample_rate,
            "threshold_volts": settings.threshold_volts,
            "capture_duration_s": settings.duration_s,
            "observable": spec.observable if spec else "",
            "outlier_band": settings.outlier_band,
            "trace_bin_ms": settings.bin_ms,
            "row_resolution": (
                f"{settings.bin_ms:g} ms bins" if settings.bin_ms > 0
                else "one row per capture"
            ),
            "reset_high_us": settings.reset_high_us,
            "reset_low_us": settings.reset_low_us,
        },
        "calibration": {
            "model": cal.describe(),
            "a_hz_per_k": cal.a,
            "b_hz": cal.b,
            "points": [
                {"temp_c": p.temp_c, "rate_hz": p.rate_hz, "note": p.note}
                for p in cal.points
            ],
        },
        "instruments": instruments or {},
        # The sweep plan belongs here: without it a chamber run cannot be
        # interpreted later. Reconstructing soak and dwell from the data after
        # the fact is possible but wasteful - it took a tolerance sweep across
        # the whole file to recover that soak+dwell had been 91.8 s.
        "sweep": sweep,
        "columns": COLUMNS,
    }


def _safe(fn):
    """Best-effort lookup; a missing username must not abort a recording."""
    try:
        return fn()
    except Exception:
        return None


class TemperatureRecorder:
    """Append-and-flush CSV writer for :class:`Reading` values."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh: Optional[TextIO] = None
        self._writer = None
        self.rows = 0
        self.t0 = 0.0
        self._capture = 0
        self._keys = []
        self.columns = list(COLUMNS)
        #: Absolute time of the first row written. t_rel_s is measured from
        #: here, not from the button press: the capture in flight when Record
        #: was pressed began earlier, which would otherwise make the first
        #: timestamps negative.
        self._t_first: Optional[float] = None
        self.provenance: dict = {}
        #: Extra trailing columns whose value is stamped onto every row from a
        #: live source outside the reading itself - the chamber setpoint and
        #: actual temperature during a sweep. Names are fixed at start(); the
        #: values are read fresh from ``extra_values`` for each row so a slow
        #: chamber poll simply repeats until it updates.
        self.extra_columns: List[str] = []
        self.extra_values: dict = {}

    # ------------------------------------------------------------- lifecycle
    @property
    def active(self) -> bool:
        return self._fh is not None

    @property
    def sidecar_path(self) -> str:
        """JSON sidecar next to the CSV: ``run.csv`` -> ``run.meta.json``."""
        base, _ = os.path.splitext(self.path)
        return base + ".meta.json"

    def update_sweep(self, sweep: Optional[dict]) -> None:
        """Attach a sweep plan to a recording already in progress.

        Recording is normally started before the sweep, so the plan is not
        known at start(); this rewrites the sidecar rather than losing it.
        """
        if not self.active:
            return
        self.provenance["sweep"] = sweep
        self._write_sidecar()

    def start(
        self,
        settings: AcquireSettings,
        cal: Calibration,
        instruments: Optional[dict] = None,
        sweep: Optional[dict] = None,
    ) -> None:
        if self.active:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        self._fh = open(self.path, "w", newline="")
        self.t0 = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(self.t0))

        self.provenance = build_provenance(settings, cal, self.t0, instruments,
                                           sweep=sweep)
        self.provenance["created"]["local"] = stamp
        self._write_sidecar()

        # Deliberately NO '#' banner in the CSV: a comment header makes the file
        # unreadable to cicwave unless --csv-comment '#' is passed, and the MCP
        # plot tool has no way to pass it. The data file stays a plain header row
        # plus numbers so any tool opens it with no flags; all provenance lives
        # in the JSON sidecar beside it.
        self._keys = list(settings.sensor_keys)
        if len(self._keys) > 1:
            cols = ["t_rel_s", "t_unix"]
            for k in self._keys:
                cols += [f"{k}_temp_c", f"{k}_sem_c", f"{k}_rate_hz", f"{k}_events"]
            cols.append("capture")
        else:
            cols = list(COLUMNS)
        cols += list(self.extra_columns)
        self.columns = cols
        self.provenance["columns"] = cols
        self._write_sidecar()
        self._writer = csv.writer(self._fh)
        self._writer.writerow(cols)
        self._fh.flush()
        self.rows = 0
        self._capture = 0
        self._t_first = None

    def _emit_row(self, row) -> None:
        """Write one data row, appending the live extra columns to it."""
        if self.extra_columns:
            row = list(row) + [self.extra_values.get(c, "") for c in self.extra_columns]
        self._writer.writerow(row)
        self.rows += 1

    def add(self, reading) -> None:
        """Append a reading at trace-bin resolution.

        Accepts one Reading, or a {sensor: Reading} dict from dual mode. Both
        sensors come out of the same capture and so share bin edges, which lets
        them be written as aligned wide columns rather than a long format that
        would need pivoting to plot.

        One row per bin when the capture was binned, otherwise a single row for
        the capture mean. Empty captures are ignored.
        """
        if not self.active:
            return
        import numpy as np

        if isinstance(reading, dict):
            self._add_multi(reading, np)
            return
        if not reading.ok:
            return

        # t_wall is stamped after reduction, so the capture began roughly
        # duration_s earlier; bin centres are relative to that start.
        start_unix = reading.t_wall - reading.duration_s
        if self._t_first is None:
            self._t_first = start_unix
        start_rel = start_unix - self._t_first

        if reading.bin_t.size:
            counts = np.maximum(reading.bin_n, 1.0)
            sem = reading.std_temp_c / np.sqrt(counts)
            rows = zip(
                start_rel + reading.bin_t,
                start_unix + reading.bin_t,
                reading.bin_temp_c,
                sem,
                reading.bin_rate_hz,
                reading.bin_n,
            )
            for t_rel, t_abs, temp, se, rate, n in rows:
                if not np.isfinite(rate):
                    continue  # empty bin: no events, nothing measured
                self._emit_row(
                    [
                        f"{t_rel:.6f}", f"{t_abs:.6f}",
                        f"{temp:.5f}" if np.isfinite(temp) else "",
                        f"{se:.5f}" if np.isfinite(se) else "",
                        f"{rate:.4f}",
                        f"{1e9 / rate:.4f}" if rate else "",
                        int(n),
                        self._capture,
                    ]
                )
        else:
            sem = reading.std_temp_c / max(1.0, np.sqrt(reading.n))
            self._emit_row(
                [
                    f"{reading.t_wall - self._t_first:.6f}", f"{reading.t_wall:.6f}",
                    f"{reading.mean_temp_c:.5f}", f"{sem:.5f}",
                    f"{reading.mean_rate_hz:.4f}", f"{reading.mean_s * 1e9:.4f}",
                    reading.n, self._capture,
                ]
            )

        self._capture += 1
        # Flush every capture: a recording is worthless if a crash loses the tail.
        self._fh.flush()

    def _add_multi(self, readings: dict, np) -> None:
        """Write one aligned row per bin covering every sensor in the capture."""
        keys = [k for k in self._keys if k in readings and readings[k].ok]
        if not keys:
            return
        base = readings[keys[0]]
        start_unix = base.t_wall - base.duration_s
        if self._t_first is None:
            self._t_first = start_unix
        start_rel = start_unix - self._t_first
        nbins = min(readings[k].bin_t.size for k in keys)

        if nbins == 0:  # binning off: one row for the capture means
            row = [f"{start_rel:.6f}", f"{start_unix:.6f}"]
            for k in keys:
                r = readings[k]
                sem = r.std_temp_c / max(1.0, np.sqrt(r.n))
                row += [f"{r.mean_temp_c:.5f}", f"{sem:.5f}", f"{r.mean_rate_hz:.4f}", r.n]
            row.append(self._capture)
            self._emit_row(row)
        else:
            for i in range(nbins):
                rates = [readings[k].bin_rate_hz[i] for k in keys]
                if not all(np.isfinite(v) for v in rates):
                    continue  # a bin with no events in one sensor is not aligned
                row = [
                    f"{start_rel + base.bin_t[i]:.6f}",
                    f"{start_unix + base.bin_t[i]:.6f}",
                ]
                for k, rate in zip(keys, rates):
                    r = readings[k]
                    n = max(r.bin_n[i], 1.0)
                    row += [
                        f"{r.bin_temp_c[i]:.5f}" if np.isfinite(r.bin_temp_c[i]) else "",
                        f"{r.std_temp_c / np.sqrt(n):.5f}",
                        f"{rate:.4f}",
                        int(r.bin_n[i]),
                    ]
                row.append(self._capture)
                self._emit_row(row)

        self._capture += 1
        self._fh.flush()

    def _write_sidecar(self) -> None:
        """(Re)write the JSON sidecar. Never let this kill a recording."""
        try:
            with open(self.sidecar_path, "w") as fh:
                json.dump(self.provenance, fh, indent=2)
        except OSError:
            pass

    def stop(self) -> tuple[str, int, float]:
        """Close the file and return ``(path, rows, duration_s)``."""
        if not self.active:
            return (self.path, 0, 0.0)
        duration = time.time() - self.t0
        self._fh.close()
        self._fh = None
        self._writer = None
        # Close out the sidecar with what the run actually produced, so it
        # describes the finished file rather than only its intent.
        self.provenance["result"] = {
            "rows": self.rows,
            "duration_s": duration,
            "ended_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "csv": os.path.basename(self.path),
        }
        self._write_sidecar()
        return (self.path, self.rows, duration)

    def elapsed(self) -> float:
        return time.time() - self.t0 if self.active else 0.0

    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0


#: Per-event columns written by :func:`export_capture`.
CAPTURE_COLUMNS = ["t_s", "observable_s", "observable_ns", "rate_hz", "temp_c"]


def export_capture(
    path: str,
    reading: Reading,
    settings: AcquireSettings,
    cal: Calibration,
    instruments: Optional[dict] = None,
) -> tuple[str, int]:
    """Write one capture's *per-event* data, plus a provenance sidecar.

    This is the raw view - every period or pulse in the capture, not the
    per-capture average the recorder writes. It is what shows GR07's
    clock-retiming staircase directly.

    The format follows the extension: ``.csv`` for anything, or ``.parquet`` /
    ``.feather`` which cicwave also reads and which matter here because a 1 s
    capture is ~900k events (a ~60 MB CSV, but a few MB binary).
    """
    if not reading.ok:
        raise ValueError("capture is empty, nothing to export")

    import numpy as np

    ns = reading.event_s * 1e9
    temp = reading.temp_c
    if temp.size != reading.event_s.size:
        temp = np.full(reading.event_s.size, np.nan)

    ext = os.path.splitext(path)[1].lower()
    n = int(reading.event_s.size)

    if ext in (".parquet", ".feather", ".h5", ".hdf5"):
        try:
            import pandas as pd
        except ImportError as exc:
            raise ValueError(f"{ext} export needs pandas installed") from exc
        df = pd.DataFrame(
            {
                "t_s": reading.event_t,
                "observable_s": reading.event_s,
                "observable_ns": ns,
                "rate_hz": reading.rate_hz,
                "temp_c": temp,
            }
        )
        if ext == ".parquet":
            df.to_parquet(path, index=False)
        elif ext == ".feather":
            df.to_feather(path)
        else:
            df.to_hdf(path, key="capture", mode="w")
    else:
        # Plain header row + numbers, no comment banner - see the note in
        # TemperatureRecorder.start about cicwave and '#'.
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(CAPTURE_COLUMNS)
            for i in range(n):
                w.writerow(
                    [
                        f"{reading.event_t[i]:.12e}",
                        f"{reading.event_s[i]:.12e}",
                        f"{ns[i]:.6f}",
                        f"{reading.rate_hz[i]:.6f}",
                        f"{temp[i]:.6f}",
                    ]
                )

    meta = build_provenance(settings, cal, reading.t_wall, instruments)
    meta["format"] = "jnwtemp-capture/1"
    meta["columns"] = CAPTURE_COLUMNS
    meta["result"] = {
        "events": n,
        "rejected": reading.n_rejected,
        "capture_s": reading.duration_s,
        "sample_rate_hz": reading.sample_rate,
        "mean_observable_s": reading.mean_s,
        "mean_rate_hz": reading.mean_rate_hz,
        "mean_temp_c": reading.mean_temp_c,
        "data_file": os.path.basename(path),
    }
    side = os.path.splitext(path)[0] + ".meta.json"
    try:
        with open(side, "w") as fh:
            json.dump(meta, fh, indent=2)
    except OSError:
        pass
    return (path, n)
