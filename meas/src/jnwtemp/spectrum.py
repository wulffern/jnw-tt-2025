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
"""Noise spectra and a long-running history for the measured temperature.

Two very different time scales are interesting and both are offered:

* *within a capture* - the sensor emits ~1e6 periods/s, so a single 50 ms
  capture is a 50k-point record sampled at ~1 MHz. Its spectrum is the
  conversion noise / jitter of the sensor itself.
* *across captures* - one averaged reading per capture, accumulated for as long
  as the app runs. Its spectrum shows drift and 1/f, which is what actually
  limits a temperature measurement over minutes.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

import numpy as np

try:
    from scipy import signal as _sp_signal
except ImportError:  # pragma: no cover - scipy is a hard dependency in practice
    _sp_signal = None


def welch_psd(
    x: np.ndarray, fs: float, nperseg: Optional[int] = None
) -> Tuple[np.ndarray, np.ndarray]:
    """Single-sided PSD in units^2/Hz, DC bin dropped.

    Falls back to a plain Hann-windowed periodogram if scipy is unavailable.
    """
    x = np.asarray(x, dtype=float)
    if x.size < 8 or not np.isfinite(fs) or fs <= 0:
        return np.empty(0), np.empty(0)
    x = x - x.mean()

    if nperseg is None:
        nperseg = int(2 ** np.floor(np.log2(max(16, min(x.size, 8192)))))
    nperseg = min(nperseg, x.size)

    if _sp_signal is not None:
        # Mean removal only: a per-segment linear detrend would subtract exactly
        # the slow drift the long-term spectrum exists to show (and scipy's
        # linear detrend goes numerically sour on segments this long).
        f, p = _sp_signal.welch(x, fs=fs, nperseg=nperseg, detrend="constant")
    else:
        w = np.hanning(x.size)
        spec = np.fft.rfft(x * w)
        p = (np.abs(spec) ** 2) / (fs * (w**2).sum())
        p[1:-1] *= 2
        f = np.fft.rfftfreq(x.size, 1.0 / fs)
    keep = f > 0
    return f[keep], p[keep]


def amplitude_spectrum(x: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray]:
    """Hann-windowed single-sided amplitude spectrum, DC bin dropped."""
    x = np.asarray(x, dtype=float)
    if x.size < 8 or not np.isfinite(fs) or fs <= 0:
        return np.empty(0), np.empty(0)
    x = x - x.mean()
    w = np.hanning(x.size)
    spec = np.fft.rfft(x * w) / (w.sum() / 2)
    f = np.fft.rfftfreq(x.size, 1.0 / fs)
    return f[1:], np.abs(spec)[1:]


def integrated_noise(f: np.ndarray, psd: np.ndarray) -> float:
    """RMS obtained by integrating a PSD over its whole band."""
    if f.size < 2:
        return float("nan")
    return float(np.sqrt(np.trapezoid(psd, f)))


def allan_deviation(x: np.ndarray, dt: float, n_taus: int = 24) -> Tuple[np.ndarray, np.ndarray]:
    """Overlapping Allan deviation - the honest way to read drift vs averaging.

    Tells you how much averaging actually helps: the curve falls as 1/sqrt(tau)
    while white noise dominates and turns up again once drift takes over.
    """
    x = np.asarray(x, dtype=float)
    n = x.size
    if n < 16 or dt <= 0:
        return np.empty(0), np.empty(0)
    max_m = n // 4
    ms = np.unique(np.round(np.logspace(0, np.log10(max(2, max_m)), n_taus)).astype(int))
    ms = ms[(ms >= 1) & (ms <= max_m)]

    theta = np.concatenate(([0.0], np.cumsum(x))) * dt  # phase-like integral
    taus, devs = [], []
    for m in ms:
        k = n - 2 * m
        if k < 1:
            continue
        d = theta[2 * m : 2 * m + k] - 2 * theta[m : m + k] + theta[:k]
        tau = m * dt
        var = np.sum(d**2) / (2 * k * tau**2)
        taus.append(tau)
        devs.append(np.sqrt(var))
    return np.array(taus), np.array(devs)


class History:
    """Bounded ring buffer of (time, value) samples for the scrolling plots."""

    def __init__(self, maxlen: int = 200_000) -> None:
        self.t: Deque[float] = deque(maxlen=maxlen)
        self.v: Deque[float] = deque(maxlen=maxlen)

    def append(self, t: float, v: float) -> None:
        self.t.append(float(t))
        self.v.append(float(v))

    def extend(self, t, v) -> None:
        self.t.extend(np.asarray(t, dtype=float).tolist())
        self.v.extend(np.asarray(v, dtype=float).tolist())

    def clear(self) -> None:
        self.t.clear()
        self.v.clear()

    def arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        return np.fromiter(self.t, dtype=float), np.fromiter(self.v, dtype=float)

    def __len__(self) -> int:
        return len(self.t)

    def mean_dt(self) -> float:
        """Average sample spacing, used as the fs for the long-term spectrum."""
        if len(self.t) < 2:
            return float("nan")
        return (self.t[-1] - self.t[0]) / (len(self.t) - 1)
