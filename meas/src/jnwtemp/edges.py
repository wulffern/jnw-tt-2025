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
"""Saleae binary digital export: parsing and edge-timing measurements.

Logic 2 stores digital channels as *transitions*, not samples, so a 1 MHz square
wave captured for 200 ms is ~400k doubles (3 MB) regardless of the 500 MS/s
sample rate. That is what makes a live loop at the top sample rate practical.

Binary format (Saleae "Binary export", version 0, digital)::

    char     identifier[8]   // "<SALEAE>"
    int32    version         // 0
    int32    type            // 0 = digital, 1 = analog
    uint32   initial_state   // level before the first transition
    double   begin_time
    double   end_time
    uint64   num_transitions
    double   transition_times[num_transitions]
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

_MAGIC = b"<SALEAE>"
_HEADER = struct.Struct("<8siiIddQ")


@dataclass
class EdgeTrain:
    """All transitions of one digital channel over one capture."""

    initial_state: int
    begin_time: float
    end_time: float
    transitions: np.ndarray  # absolute times [s] of every edge, ascending

    @property
    def duration(self) -> float:
        return self.end_time - self.begin_time

    @property
    def num_edges(self) -> int:
        return int(self.transitions.size)

    # The level *after* transition i is (initial_state + i + 1) % 2, so the
    # rising edges are the even-indexed ones when the channel starts low.
    def _edges(self, rising: bool) -> np.ndarray:
        start = 0 if (self.initial_state == 0) == rising else 1
        return self.transitions[start::2]

    def rising(self) -> np.ndarray:
        return self._edges(True)

    def falling(self) -> np.ndarray:
        return self._edges(False)

    def periods(self) -> tuple[np.ndarray, np.ndarray]:
        """Rising-edge-to-rising-edge periods.

        Returns ``(t, period)`` where ``t`` is the time of the rising edge that
        opened each period, so the result can be plotted against wall time.
        """
        r = self.rising()
        if r.size < 2:
            return np.empty(0), np.empty(0)
        return r[:-1], np.diff(r)

    def high_widths(self) -> tuple[np.ndarray, np.ndarray]:
        """Width of every complete high pulse, keyed by its rising edge."""
        r, f = self.rising(), self.falling()
        if r.size == 0 or f.size == 0:
            return np.empty(0), np.empty(0)
        if f[0] < r[0]:  # drop a leading partial pulse
            f = f[1:]
        n = min(r.size, f.size)
        if n == 0:
            return np.empty(0), np.empty(0)
        return r[:n], f[:n] - r[:n]

    def low_widths(self) -> tuple[np.ndarray, np.ndarray]:
        """Width of every complete low pulse, keyed by its falling edge."""
        r, f = self.rising(), self.falling()
        if r.size == 0 or f.size == 0:
            return np.empty(0), np.empty(0)
        if r[0] < f[0]:
            r = r[1:]
        n = min(r.size, f.size)
        if n == 0:
            return np.empty(0), np.empty(0)
        return f[:n], r[:n] - f[:n]

    def duty(self) -> float:
        """Fraction of the capture spent high (NaN if there are no edges)."""
        if self.transitions.size == 0:
            return float(self.initial_state)
        _, w = self.high_widths()
        if w.size == 0:
            return float("nan")
        return float(w.sum() / self.duration)


def read_binary(path: str) -> EdgeTrain:
    """Parse one ``digital_N.bin`` produced by ``export_raw_data_binary``."""
    with open(path, "rb") as fh:
        blob = fh.read()
    if len(blob) < _HEADER.size:
        raise ValueError(f"{path}: too short to be a Saleae binary export")
    magic, version, kind, initial, t0, t1, count = _HEADER.unpack_from(blob, 0)
    if magic != _MAGIC:
        raise ValueError(f"{path}: bad magic {magic!r}, expected {_MAGIC!r}")
    if kind != 0:
        raise ValueError(f"{path}: not a digital export (type={kind})")
    if version != 0:
        raise ValueError(f"{path}: unsupported binary export version {version}")
    times = np.frombuffer(blob, dtype="<f8", count=int(count), offset=_HEADER.size)
    return EdgeTrain(int(initial), float(t0), float(t1), times)


def robust_stats(x: np.ndarray, sigma: float = 5.0) -> tuple[np.ndarray, dict]:
    """Drop outliers more than ``sigma`` MAD-sigmas from the median.

    A free-running sensor output picks up the occasional glitch or missed edge,
    and a single 2x period would otherwise dominate both the mean and the FFT.
    """
    if x.size == 0:
        return x, {"n": 0, "n_rejected": 0}
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    scale = 1.4826 * mad
    if scale <= 0:
        keep = np.ones(x.size, dtype=bool)
    else:
        keep = np.abs(x - med) <= sigma * scale
    clean = x[keep]
    if clean.size == 0:
        clean = x
        keep = np.ones(x.size, dtype=bool)
    return clean, {
        "n": int(clean.size),
        "n_rejected": int(x.size - clean.size),
        "median": med,
        "mean": float(clean.mean()),
        "std": float(clean.std(ddof=1)) if clean.size > 1 else 0.0,
    }
