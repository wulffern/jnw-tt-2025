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
"""Turning an edge timing into a temperature.

Both sensors on JNW-TEMP are PTAT-current-into-a-capacitor ramps compared
against a fixed threshold, so the *time* they produce is

    t = V_ref * C / I(T),    I(T) = k*T*ln(N) / (q*R)

which makes ``t`` inversely proportional to absolute temperature and the
corresponding *rate* ``r = 1/t`` proportional to it:

    r = a * T_K   (ideal PTAT, one free parameter)

The GR07 output on uo_out[0] is a free-running PWM whose period is that ramp
time re-timed by the project clock, so its period is the observable. The GR06
output on uo_out[2] is a single pulse after each ResetTemp06 release, so its
high time is the observable. Either way the model below sees a rate in Hz.

A real circuit has an offset (comparator delay, reset time, clock re-timing),
so an affine fit ``r = a*T_K + b`` is supported too; it needs measurements at
two known temperatures. With a single reference point we fall back to the ideal
proportional model, which is the honest choice at one calibration point.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

import numpy as np

KELVIN = 273.15

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "jnwtemp", "calibration.json"
)


@dataclass
class CalPoint:
    """One (known temperature, measured rate) observation."""

    temp_c: float
    rate_hz: float
    note: str = ""


@dataclass
class Calibration:
    """Rate-to-temperature model for one sensor."""

    sensor: str = "GR07"
    #: r = a*T_K + b, with a in Hz/K and b in Hz.
    a: Optional[float] = None
    b: float = 0.0
    points: List[CalPoint] = field(default_factory=list)

    # ---------------------------------------------------------------- model
    @property
    def calibrated(self) -> bool:
        return self.a is not None and self.a != 0.0

    def temp_c(self, rate_hz):
        """Convert a rate (or array of rates) in Hz to degrees Celsius."""
        if not self.calibrated:
            return np.full_like(np.asarray(rate_hz, dtype=float), np.nan)
        return (np.asarray(rate_hz, dtype=float) - self.b) / self.a - KELVIN

    def rate_hz(self, temp_c: float) -> float:
        """Inverse of :meth:`temp_c`, for drawing the model on a plot."""
        if not self.calibrated:
            return float("nan")
        return self.a * (temp_c + KELVIN) + self.b

    def sensitivity_hz_per_k(self) -> float:
        return float(self.a) if self.calibrated else float("nan")

    # ------------------------------------------------------------ fitting
    def add_point(self, temp_c: float, rate_hz: float, note: str = "") -> None:
        """Record a calibration observation and refit.

        Points within 0.5 K of an existing one replace it, so repeatedly
        calibrating at room temperature does not stack up duplicates.
        """
        self.points = [p for p in self.points if abs(p.temp_c - temp_c) > 0.5]
        self.points.append(CalPoint(float(temp_c), float(rate_hz), note))
        self.points.sort(key=lambda p: p.temp_c)
        self.refit()

    def refit(self) -> None:
        pts = self.points
        if not pts:
            self.a, self.b = None, 0.0
        elif len(pts) == 1:
            # Ideal PTAT through the origin: one point sets the slope.
            p = pts[0]
            self.a = p.rate_hz / (p.temp_c + KELVIN)
            self.b = 0.0
        else:
            tk = np.array([p.temp_c + KELVIN for p in pts])
            r = np.array([p.rate_hz for p in pts])
            a, b = np.polyfit(tk, r, 1)
            self.a, self.b = float(a), float(b)

    def clear(self) -> None:
        self.points = []
        self.a, self.b = None, 0.0

    def describe(self) -> str:
        if not self.calibrated:
            return "uncalibrated"
        model = "1-point PTAT" if len(self.points) == 1 else f"{len(self.points)}-point fit"
        return f"{model}: r = {self.a:.4g}*T_K + {self.b:.4g} Hz  ({self.a/1e3:.3f} kHz/K)"

    # -------------------------------------------------------------- storage
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        pts = [CalPoint(**p) for p in d.get("points", [])]
        return cls(
            sensor=d.get("sensor", "GR07"),
            a=d.get("a"),
            b=d.get("b", 0.0),
            points=pts,
        )


class CalibrationStore:
    """Per-sensor calibrations persisted as one small JSON file."""

    def __init__(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        self.path = path
        self.cals = {}
        self.load()

    def get(self, sensor: str) -> Calibration:
        if sensor not in self.cals:
            self.cals[sensor] = Calibration(sensor=sensor)
        return self.cals[sensor]

    def load(self) -> None:
        try:
            with open(self.path) as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        for sensor, d in raw.items():
            try:
                self.cals[sensor] = Calibration.from_dict(d)
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump({s: c.to_dict() for s, c in self.cals.items()}, fh, indent=2)


def resolution_k(rate_hz: float, timing_lsb_s: float, cal: Calibration) -> float:
    """Temperature resolution implied by one timing quantum.

    ``timing_lsb_s`` is whichever quantum dominates: the Saleae sample period
    (2 ns at 500 MS/s) or, for GR07, the project clock period that re-times the
    comparator output (15.6 ns at 64 MHz).
    """
    if not cal.calibrated or rate_hz <= 0:
        return float("nan")
    # r = 1/t, so dr = -dt/t^2 = -dr^2*dt, and dT = dr/a.
    return float(rate_hz * rate_hz * timing_lsb_s / abs(cal.a))
