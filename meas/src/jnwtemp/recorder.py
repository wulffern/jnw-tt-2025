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
from typing import Optional, TextIO

from . import __version__
from .acquire import SENSORS, AcquireSettings, Reading
from .temperature import Calibration

COLUMNS = [
    "t_rel_s",           # seconds since the recording started
    "t_unix",            # absolute time, so runs can be correlated with anything else
    "temp_c",            # capture-mean temperature
    "temp_sem_c",        # standard error of that mean
    "temp_sigma_c",      # per-event spread within the capture
    "rate_hz",           # the raw observable, in case the model changes later
    "observable_s",
    "observable_sigma_s",
    "n_events",
    "n_rejected",
    "capture_s",
    "sample_rate_hz",
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
        self.provenance: dict = {}

    # ------------------------------------------------------------- lifecycle
    @property
    def active(self) -> bool:
        return self._fh is not None

    @property
    def sidecar_path(self) -> str:
        """JSON sidecar next to the CSV: ``run.csv`` -> ``run.meta.json``."""
        base, _ = os.path.splitext(self.path)
        return base + ".meta.json"

    def start(
        self,
        settings: AcquireSettings,
        cal: Calibration,
        instruments: Optional[dict] = None,
    ) -> None:
        if self.active:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        self._fh = open(self.path, "w", newline="")
        self.t0 = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(self.t0))

        self.provenance = build_provenance(settings, cal, self.t0, instruments)
        self.provenance["created"]["local"] = stamp
        self._write_sidecar()

        # Deliberately NO '#' banner in the CSV: a comment header makes the file
        # unreadable to cicwave unless --csv-comment '#' is passed, and the MCP
        # plot tool has no way to pass it. The data file stays a plain header row
        # plus numbers so any tool opens it with no flags; all provenance lives
        # in the JSON sidecar beside it.
        self._writer = csv.writer(self._fh)
        self._writer.writerow(COLUMNS)
        self._fh.flush()
        self.rows = 0

    def add(self, reading: Reading) -> None:
        """Append one reading. Silently ignores empty captures."""
        if not self.active or not reading.ok:
            return
        import numpy as np

        sem = reading.std_temp_c / max(1.0, np.sqrt(reading.n))
        self._writer.writerow(
            [
                f"{reading.t_wall - self.t0:.6f}",
                f"{reading.t_wall:.6f}",
                f"{reading.mean_temp_c:.6f}",
                f"{sem:.6f}",
                f"{reading.std_temp_c:.6f}",
                f"{reading.mean_rate_hz:.6f}",
                f"{reading.mean_s:.12e}",
                f"{reading.std_s:.12e}",
                reading.n,
                reading.n_rejected,
                f"{reading.duration_s:.6f}",
                reading.sample_rate,
            ]
        )
        # Flush every row: a recording is worthless if a crash loses the tail.
        self._fh.flush()
        self.rows += 1

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
