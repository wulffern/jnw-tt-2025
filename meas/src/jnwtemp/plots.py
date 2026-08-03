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
"""Live plot widgets.

Each plot carries a single series, so the title names it and no legend box is
needed; identity never rests on color alone. Colors are the first three
categorical slots stepped for a dark surface, which validate all-pairs in dark
mode. Grid and axes stay recessive, lines are 2px, and every plot has a
crosshair readout so values can be read off directly rather than estimated.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

# --- dark-surface tokens ----------------------------------------------------
SURFACE = "#1a1a19"
SURFACE_2 = "#232322"
TEXT_PRIMARY = "#ffffff"
TEXT_SECONDARY = "#c3c2b7"
TEXT_MUTED = "#8a8980"
GRID = "#383835"

SERIES_1 = "#3987e5"  # blue   - temperature
SERIES_2 = "#d95926"  # orange - spectra
SERIES_3 = "#199e70"  # aqua   - raw timing observable
#: Colour follows the sensor everywhere - trace, spectrum, tiles - so a colour
#: always means the same circuit.
SENSOR_COLORS = {"GR07": SERIES_1, "GR06": SERIES_2}

STATUS_GOOD = "#199e70"
STATUS_WARN = "#c98500"
STATUS_BAD = "#e66767"

pg.setConfigOption("background", SURFACE)
pg.setConfigOption("foreground", TEXT_SECONDARY)
pg.setConfigOptions(antialias=True)


#: Points beyond this per curve stall the Qt paint loop. One capture holds
#: ~180k periods, far more than a ~1200 px wide plot can show, and painting
#: them all blocks the GUI thread for seconds.
#:
#: The envelope spends two points per bin, so this is ~1000 columns against a
#: plot about 1100 px wide - already one bin per pixel. Measured on the live
#: trace: 4000 points cost 34 ms a repaint, 2000 cost 23, and at 8 updates a
#: second that difference is the whole feel of the window.
MAX_PLOT_POINTS = 2000

#: The live trace is redrawn several times a second and is read for shape and
#: level, not for the smoothness of its diagonals - and antialiasing a curve
#: this long costs more than halving its points does (34 ms -> 14 ms at 4000).
#: The spectra keep it: they are redrawn rarely and are read closely.
TRACE_ANTIALIAS = False


def _decimate_block(x, y, max_points):
    """Min/max envelope thinning of a gap-free block.

    Vectorised deliberately: this runs on every repaint, for every curve, over
    the whole history. A Python loop over the bins costs a couple of thousand
    iterations per redraw, which is invisible at one capture per second and
    stalls the GUI at ten.
    """
    n = y.size
    if n <= max_points or max_points < 2:
        return x, y
    bins = max(1, max_points // 2)
    step = n // bins                       # >= 1, since bins <= n here
    head = y[:bins * step].reshape(bins, step)
    base = np.arange(bins) * step
    idx = np.concatenate((
        head.argmin(axis=1) + base,
        head.argmax(axis=1) + base,
        np.arange(bins * step, n),         # whatever the reshape left over
    ))
    idx = np.unique(idx)                   # sorted, so still in time order
    return x[idx], y[idx]


def envelope_decimate(
    x: np.ndarray, y: np.ndarray, max_points: int = MAX_PLOT_POINTS
) -> Tuple[np.ndarray, np.ndarray]:
    """Thin a series to ~``max_points`` while keeping its min/max envelope.

    Plain slicing would drop exactly the outliers worth seeing, so each output
    bin contributes its minimum and its maximum, in time order. What is drawn
    then still spans the true range of the data at every x.

    NaNs mark real gaps in the record - the dead time between captures - and
    must survive thinning, otherwise the curve is drawn straight across a period
    when nothing was measured. So the series is split on NaN, each block is
    thinned on its own budget, and the breaks are put back.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if y.size == 0:
        return x, y

    finite = np.isfinite(y)
    if finite.all():
        return _decimate_block(x, y, max_points)

    # Contiguous runs of finite samples, i.e. one per capture.
    edges = np.flatnonzero(np.diff(finite.astype(np.int8)))
    starts = np.concatenate(([0], edges + 1))
    stops = np.concatenate((edges + 1, [y.size]))
    blocks = [(a, b) for a, b in zip(starts, stops) if finite[a]]
    if not blocks:
        return x[:0], y[:0]

    total = sum(b - a for a, b in blocks)
    out_x, out_y = [], []
    for k, (a, b) in enumerate(blocks):
        share = max(4, int(max_points * (b - a) / total))
        bx, by = _decimate_block(x[a:b], y[a:b], share)
        if k:
            # One NaN between blocks is all a "connect=finite" curve needs.
            out_x.append(np.array([x[a - 1] if a else x[a]]))
            out_y.append(np.array([np.nan]))
        out_x.append(bx)
        out_y.append(by)
    return np.concatenate(out_x), np.concatenate(out_y)


class DecadeLogAxis(pg.AxisItem):
    """Log axis that labels only whole decades.

    Over the ~6 decades a noise spectrum spans, pyqtgraph's default also labels
    intermediate ticks (2*10^5, 3*10^5 ...) and they collide into an unreadable
    smear. Only decades are labelled here; the minor ticks stay as tick marks.
    """

    def logTickStrings(self, values, scale, spacing):  # noqa: N802 (pyqtgraph API)
        out = []
        for v in values:
            nearest = round(v)
            out.append(f"1e{int(nearest)}" if abs(v - nearest) < 1e-6 else "")
        return out


def _style_axes(plot: pg.PlotItem) -> None:
    plot.showGrid(x=True, y=True, alpha=0.18)
    for name in ("left", "bottom"):
        ax = plot.getAxis(name)
        ax.setPen(pg.mkPen(GRID, width=1))
        ax.setTextPen(pg.mkPen(TEXT_MUTED))
    plot.getViewBox().setDefaultPadding(0.02)


class CrosshairPlot(pg.PlotWidget):
    """A plot with a crosshair and a value readout in the corner.

    Interaction is the default, not an extra: an instrument plot is read for
    specific values, so the cursor always reports the point under it.
    """

    def __init__(
        self,
        title: str,
        x_label: str,
        y_label: str,
        color: str = SERIES_1,
        fmt: str = "{x:.3f}, {y:.3f}",
        log_x: bool = False,
        log_y: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        axis_items = {}
        if log_x:
            axis_items["bottom"] = DecadeLogAxis(orientation="bottom")
        if log_y:
            axis_items["left"] = DecadeLogAxis(orientation="left")
        super().__init__(parent, axisItems=axis_items or None)
        self._fmt = fmt
        self.setTitle(title, color=TEXT_SECONDARY, size="10pt")
        self.setLabel("bottom", x_label, color=TEXT_MUTED)
        self.setLabel("left", y_label, color=TEXT_MUTED)
        self.setLogMode(x=log_x, y=log_y)
        _style_axes(self.getPlotItem())

        self.curve = self.plot([], [], pen=pg.mkPen(color, width=2))
        self._antialias = True

        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(TEXT_MUTED, width=1))
        self._hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(TEXT_MUTED, width=1))
        for line in (self._vline, self._hline):
            line.setVisible(False)
            self.addItem(line, ignoreBounds=True)

        self._readout = pg.TextItem(color=TEXT_PRIMARY, anchor=(0, 0))
        self._readout.setVisible(False)
        self.addItem(self._readout, ignoreBounds=True)

        self._data: Tuple[np.ndarray, np.ndarray] = (np.empty(0), np.empty(0))
        self.scene().sigMouseMoved.connect(self._on_mouse)

    # ------------------------------------------------------------------ data
    def set_data(self, x: np.ndarray, y: np.ndarray, keep_gaps: bool = False) -> None:
        """Plot ``y`` against ``x``.

        With ``keep_gaps`` the NaNs in ``y`` are kept and the curve is drawn with
        ``connect="finite"``, so breaks in the record stay visible as breaks. The
        crosshair still indexes only the finite points.
        """
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        if not keep_gaps:
            # Without gap preservation, drop non-finite points entirely.
            good = np.isfinite(x) & np.isfinite(y)
            x, y = x[good], y[good]
        # Keep the full series for the crosshair readout, but only ever hand the
        # painter an envelope-preserving thinning of it.
        finite = np.isfinite(x) & np.isfinite(y)
        self._data = (x[finite], y[finite])
        dx, dy = envelope_decimate(x, y)
        self.curve.setData(dx, dy, connect="finite" if keep_gaps else "all",
                           antialias=self._antialias)

    def clear_data(self) -> None:
        self.set_data(np.empty(0), np.empty(0))

    # ----------------------------------------------------------- interaction
    def _on_mouse(self, pos) -> None:
        x, y = self._data
        if x.size == 0 or not self.sceneBoundingRect().contains(pos):
            for item in (self._vline, self._hline, self._readout):
                item.setVisible(False)
            return
        pt = self.getPlotItem().vb.mapSceneToView(pos)
        # In log mode the view coordinates are decades, but the data are not.
        px = 10 ** pt.x() if self.getPlotItem().ctrl.logXCheck.isChecked() else pt.x()
        idx = int(np.argmin(np.abs(x - px)))
        xv, yv = float(x[idx]), float(y[idx])

        vx = np.log10(xv) if self.getPlotItem().ctrl.logXCheck.isChecked() and xv > 0 else xv
        vy = np.log10(yv) if self.getPlotItem().ctrl.logYCheck.isChecked() and yv > 0 else yv
        self._vline.setPos(vx)
        self._hline.setPos(vy)
        self._readout.setText(self._fmt.format(x=xv, y=yv))
        self._readout.setPos(vx, vy)
        for item in (self._vline, self._hline, self._readout):
            item.setVisible(True)


class StatTile(QWidget):
    """A hero number with a unit and a caption.

    The headline reading is a single value, and a single value is not a chart -
    it gets a stat tile so it can be read across the room during a demo.
    """

    def __init__(self, caption: str, unit: str, color: str = SERIES_1) -> None:
        super().__init__()
        self._unit = unit
        self.setObjectName("StatTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)

        self._caption = QLabel(caption)
        self._caption.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px; letter-spacing:1px;")

        self._value = QLabel("--")
        font = QFont()
        font.setPointSize(34)
        font.setBold(True)
        self._value.setFont(font)
        self._value.setStyleSheet(f"color:{color};")

        self._sub = QLabel("")
        self._sub.setStyleSheet(f"color:{TEXT_SECONDARY}; font-size:11px;")

        layout.addWidget(self._caption)
        layout.addWidget(self._value)
        layout.addWidget(self._sub)
        # Scope the frame to the tile itself: an unscoped QWidget rule would put
        # a border and a panel behind each of the three child labels too.
        self.setStyleSheet(
            f"QWidget#StatTile {{ background:{SURFACE_2}; border:1px solid {GRID};"
            f" border-radius:8px; }}"
            f"QWidget#StatTile QLabel {{ background:transparent; border:none; }}"
        )

    def set_value(self, value: float, sub: str = "", decimals: int = 2) -> None:
        if value is None or not np.isfinite(value):
            self._value.setText("--")
        else:
            self._value.setText(f"{value:.{decimals}f}{self._unit}")
        self._sub.setText(sub)

    def set_color(self, color: str) -> None:
        self._value.setStyleSheet(f"color:{color};")

    def set_caption(self, caption: str, unit: Optional[str] = None) -> None:
        """Relabel the tile - dual mode repurposes two of the three.

        The unit travels with the caption: a tile switched from a rate to a
        temperature must stop saying kHz.
        """
        self._caption.setText(caption)
        if unit is not None:
            self._unit = unit


#: What the trace axis is called for each unit it can carry. The trace can be
#: shown as a temperature, as the raw rate, or as an error against the rate it
#: started at, so the axis has to say which.
TRACE_AXIS_LABELS = {
    "°C": "Temperature [degC]",
    "kHz": "Sensor rate [kHz]",
    "ppm": "Frequency error [ppm]",
    "Hz": "Frequency error [Hz]",
}


class TemperaturePlot(CrosshairPlot):
    """Estimated temperature against wall-clock time, with a mean reference."""

    def __init__(self) -> None:
        super().__init__(
            title="Estimated temperature",
            x_label="Time [s]",
            y_label="Temperature [degC]",
            color=SERIES_1,
            fmt="{x:.1f} s   {y:.3f} degC",
        )
        self._antialias = TRACE_ANTIALIAS
        # A dashed mean line makes drift visible without a second series.
        self._mean = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(TEXT_MUTED, width=1, style=Qt.PenStyle.DashLine)
        )
        self._mean.setVisible(False)
        self.addItem(self._mean, ignoreBounds=True)
        self._band = pg.LinearRegionItem(
            orientation="horizontal", movable=False, brush=pg.mkBrush(57, 135, 229, 28)
        )
        self._band.setZValue(-10)
        self._band.setVisible(False)
        self.addItem(self._band, ignoreBounds=True)

        # A recording indicator drawn on the plot itself, so a demo audience can
        # see at a glance that the trace is being written to disk.
        self._rec = pg.TextItem(color=STATUS_BAD, anchor=(1, 0))
        self._rec.setText("● REC")
        self._rec.setVisible(False)
        self.addItem(self._rec, ignoreBounds=True)
        self._recording = False

    def ensure_second_curve(self) -> None:
        """Add the GR06 curve, created lazily so single-sensor mode has one series."""
        if getattr(self, "_curve2", None) is None:
            self._curve2 = self.plot([], [], pen=pg.mkPen(SERIES_2, width=2))
            self._curve2.setZValue(-1)
            self._curve2.opts["antialias"] = TRACE_ANTIALIAS

    def set_second_series(self, t: np.ndarray, temp: np.ndarray) -> None:
        self.ensure_second_curve()
        t = np.asarray(t, dtype=float)
        temp = np.asarray(temp, dtype=float)
        dx, dy = envelope_decimate(t, temp)
        self._curve2.setData(dx, dy, connect="finite")

    def clear_second_series(self) -> None:
        if getattr(self, "_curve2", None) is not None:
            self._curve2.setData([], [])

    def show_traces(self, series: dict, units=None) -> None:
        """Draw one or two named series. Shared interface with LiveWavePlot.

        One y axis here, so the label follows the first series: mixing an
        uncalibrated rate with a calibrated temperature is the caller's problem.
        """
        items = list(series.items())
        if not items:
            return
        (name, (t, v)) = items[0]
        unit = units.get(name, "") if isinstance(units, dict) else (units or "")
        if unit:
            self.setLabel("left", TRACE_AXIS_LABELS.get(unit, unit), color=TEXT_MUTED)
        self.update_series(t, v, stats=len(items) == 1)
        if len(items) > 1:
            self.set_second_series(*items[1][1])
        else:
            self.clear_second_series()

    def set_recording(self, on: bool) -> None:
        self._recording = bool(on)
        self._rec.setVisible(self._recording)

    def update_series(self, t: np.ndarray, temp: np.ndarray, stats: bool = True) -> None:
        self.set_data(t, temp, keep_gaps=True)
        good = temp[np.isfinite(temp)]
        if stats and good.size >= 2:
            m, s = float(good.mean()), float(good.std(ddof=1))
            self._mean.setPos(m)
            self._mean.setVisible(True)
            self._band.setRegion((m - s, m + s))
            self._band.setVisible(True)
        else:
            self._mean.setVisible(False)
            self._band.setVisible(False)
        if self._recording and good.size:
            # Park the badge in the top-right of the current view.
            (x0, x1), (y0, y1) = self.getPlotItem().vb.viewRange()
            self._rec.setPos(x1, y1)


#: Events shown in the raw-timing plot. Drawing a whole 90k-event capture is
#: worse than useless: every pixel column then spans the full dither range and
#: the plot is a solid block. A short slice shows the actual event-to-event
#: pattern - which for GR07 is the clock-retiming staircase.
RAW_SLICE_EVENTS = 600


class ObservablePlot(CrosshairPlot):
    """The raw timing observable of the most recent capture, event by event."""

    def __init__(self) -> None:
        super().__init__(
            title="Last capture - raw timing",
            x_label="Time within capture [ms]",
            y_label="Observable [ns]",
            color=SERIES_3,
            fmt="{x:.3f} ms   {y:.2f} ns",
        )
        self.curve.setPen(pg.mkPen(SERIES_3, width=1))
        self.curve.setSymbol("o")
        self.curve.setSymbolSize(3)
        self.curve.setSymbolBrush(pg.mkBrush(SERIES_3))
        self.curve.setSymbolPen(None)

    def update_series(self, t_s: np.ndarray, value_s: np.ndarray, label: str) -> None:
        t = np.asarray(t_s) * 1e3
        v = np.asarray(value_s) * 1e9
        total = v.size
        if total > RAW_SLICE_EVENTS:
            t, v = t[:RAW_SLICE_EVENTS], v[:RAW_SLICE_EVENTS]
            shown = f"first {RAW_SLICE_EVENTS} of {total}"
        else:
            shown = f"{total}"
        self.setTitle(
            f"Last capture - {label.lower()} ({shown} events)",
            color=TEXT_SECONDARY,
            size="10pt",
        )
        self.set_data(t, v)


class SpectrumPlot(CrosshairPlot):
    """Noise spectrum (or Allan deviation) of the temperature estimate."""

    def __init__(self) -> None:
        super().__init__(
            title="Noise spectrum",
            x_label="Frequency [Hz]",
            y_label="PSD [degC^2/Hz]",
            color=SERIES_2,
            fmt="{x:.4g} Hz   {y:.4g}",
            log_x=True,
            log_y=True,
        )

    def ensure_second_curve(self) -> None:
        if getattr(self, "_curve2", None) is None:
            self._curve2 = self.plot([], [], pen=pg.mkPen(SERIES_2, width=2))

    def set_curve_color(self, index: int, color: str) -> None:
        """Colour curve 0 or 1. Spectra carry whichever sensor is selected, so
        the pen has to follow the sensor rather than the slot."""
        if index == 0:
            self.curve.setPen(pg.mkPen(color, width=2))
        else:
            self.ensure_second_curve()
            self._curve2.setPen(pg.mkPen(color, width=2))

    def set_second_psd(self, f: np.ndarray, y: np.ndarray) -> None:
        """Overlay a second sensor's spectrum on the same axes."""
        self.ensure_second_curve()
        f = np.asarray(f, dtype=float)
        y = np.asarray(y, dtype=float)
        good = np.isfinite(f) & np.isfinite(y) & (f > 0)
        self._curve2.setData(f[good], y[good])

    def clear_second_psd(self) -> None:
        if getattr(self, "_curve2", None) is not None:
            self._curve2.setData([], [])

    def set_log_mode(self, log_x: bool, log_y: bool) -> None:
        self.setLogMode(x=log_x, y=log_y)

    def update_psd(self, f: np.ndarray, psd: np.ndarray, title: str, y_label: str) -> None:
        self.setTitle(title, color=TEXT_SECONDARY, size="10pt")
        self.setLabel("left", y_label, color=TEXT_MUTED)
        good = (f > 0) & (psd > 0)
        self.set_data(f[good], psd[good])

    def update_allan(self, taus: np.ndarray, devs: np.ndarray) -> None:
        self.setTitle("Allan deviation of temperature", color=TEXT_SECONDARY, size="10pt")
        self.setLabel("bottom", "Averaging time tau [s]", color=TEXT_MUTED)
        self.setLabel("left", "sigma(tau) [degC]", color=TEXT_MUTED)
        good = (taus > 0) & (devs > 0)
        self.set_data(taus[good], devs[good])

    def restore_frequency_axis(self) -> None:
        self.setLabel("bottom", "Frequency [Hz]", color=TEXT_MUTED)
