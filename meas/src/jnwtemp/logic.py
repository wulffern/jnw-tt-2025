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
"""Saleae Logic 2 automation: repeated short captures of digital edges.

Requires the automation server in Logic 2 (Preferences -> Automation ->
"Enable automation server", default port 10430).

The sensor periods are ~1 us and the GR06 pulse is only tens of ns, so we run
the Logic Pro at its top digital sample rate. That rate is only available with
very few channels enabled - a Logic Pro 16 allows 500 MS/s on one channel but
just 25 MS/s on eight - and the exact set depends on the model, so the rate is
negotiated with the device rather than assumed.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .edges import EdgeTrain, read_binary

DEFAULT_PORT = 10430

#: The only digital thresholds a Logic Pro accepts. 1.2 V suits both the 1.8 V
#: chip I/O and the 3.3 V level-shifted demo board headers.
THRESHOLDS_V = [1.2, 1.8, 3.3]

#: Sample rates offered in the GUI. The set a device will actually accept
#: depends on how many channels are enabled and is *discovered*, not assumed:
#: a Logic Pro 16 allows 500 MS/s on one channel but only 25 MS/s on eight.
OFFERED_RATES = [
    500_000_000,
    250_000_000,
    125_000_000,
    100_000_000,
    50_000_000,
    25_000_000,
    10_000_000,
]

_ALLOWED_RE = re.compile(r'"digital"\s*:\s*(\d+)')


def parse_allowed_rates(message: str) -> List[int]:
    """Pull the allowed digital sample rates out of Logic 2's rejection message.

    The automation API has no query for this, but when it refuses a rate it
    enumerates the legal ones for the current channel configuration.
    """
    return sorted({int(m) for m in _ALLOWED_RE.findall(message)}, reverse=True)


class LogicError(RuntimeError):
    """Raised for anything that goes wrong talking to Logic 2."""


@dataclass
class CaptureSettings:
    """Everything the acquisition loop needs to configure one capture."""

    channels: List[int] = field(default_factory=lambda: [0])
    sample_rate: int = 500_000_000
    threshold_volts: float = 1.2
    duration_s: float = 0.05
    device_id: Optional[str] = None
    port: int = DEFAULT_PORT


class LogicCapture:
    """A connection to Logic 2 that yields :class:`EdgeTrain` per channel.

    Used as a context manager; :meth:`capture` may be called repeatedly and each
    call performs one timed capture, binary-exports it and discards it.
    """

    def __init__(self, settings: CaptureSettings) -> None:
        self.settings = settings
        self._manager = None
        self._device_id: Optional[str] = None
        self._device_type: str = ""
        #: n_channels -> rates the device accepts, learned from its rejections
        self._allowed_rates: Dict[int, List[int]] = {}
        #: rate the last capture actually ran at, which may be below the request
        self.actual_sample_rate: int = settings.sample_rate
        self.last_rate_note: str = ""

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> str:
        """Connect to the automation server and pick a device.

        Returns a human readable description of the device in use.
        """
        try:
            from saleae import automation
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise LogicError(
                "the 'logic2-automation' package is not installed "
                "(pip install logic2-automation)"
            ) from exc

        try:
            self._manager = automation.Manager.connect(port=self.settings.port)
        except Exception as exc:
            raise LogicError(
                f"could not reach the Logic 2 automation server on port "
                f"{self.settings.port}. In Logic 2 open Preferences and tick "
                f"'Enable automation server', then retry. ({exc})"
            ) from exc

        devices = [d for d in self._manager.get_devices() if not d.is_simulation]
        if not devices:
            devices = self._manager.get_devices()  # fall back to the simulator
        if not devices:
            raise LogicError("Logic 2 reports no devices at all")

        wanted = self.settings.device_id
        chosen = next((d for d in devices if d.device_id == wanted), None) if wanted else None
        if chosen is None:
            if wanted:
                raise LogicError(f"device {wanted} not connected")
            chosen = devices[0]

        self._device_id = chosen.device_id
        self._device_type = str(chosen.device_type)
        sim = " (SIMULATION)" if chosen.is_simulation else ""
        return f"{self._device_type} {self._device_id}{sim}"

    def close(self) -> None:
        if self._manager is not None:
            try:
                self._manager.close()
            except Exception:
                pass
            self._manager = None

    def __enter__(self) -> "LogicCapture":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --------------------------------------------------------------- capture
    def capture(
        self, duration_s: Optional[float] = None, channels: Optional[List[int]] = None
    ) -> Dict[int, EdgeTrain]:
        """Run one timed capture and return the edges of each channel."""
        if self._manager is None:
            raise LogicError("not connected")
        from saleae import automation

        st = self.settings
        chans = list(channels if channels is not None else st.channels)
        dur = float(duration_s if duration_s is not None else st.duration_s)
        rate = self._negotiate_rate(int(st.sample_rate), len(chans))

        outdir = tempfile.mkdtemp(prefix="jnwtemp-cap-")
        try:
            for attempt in (1, 2):
                try:
                    with self._manager.start_capture(
                        device_id=self._device_id,
                        device_configuration=automation.LogicDeviceConfiguration(
                            enabled_digital_channels=chans,
                            digital_sample_rate=rate,
                            digital_threshold_volts=float(st.threshold_volts),
                        ),
                        capture_configuration=automation.CaptureConfiguration(
                            capture_mode=automation.TimedCaptureMode(duration_seconds=dur)
                        ),
                    ) as cap:
                        cap.wait()
                        cap.export_raw_data_binary(directory=outdir, digital_channels=chans)
                    break
                except automation.errors.InvalidRequestError as exc:
                    allowed = parse_allowed_rates(str(exc))
                    if attempt == 2 or not allowed:
                        raise
                    # The device just told us what it will accept for this
                    # channel count; remember it and retry once.
                    self._allowed_rates[len(chans)] = allowed
                    new_rate = self._pick_rate(allowed, rate)
                    self.last_rate_note = (
                        f"{rate/1e6:.0f} MS/s not available with {len(chans)} channels, "
                        f"using {new_rate/1e6:.0f} MS/s"
                    )
                    rate = new_rate

            self.actual_sample_rate = rate
            trains: Dict[int, EdgeTrain] = {}
            for ch in chans:
                path = os.path.join(outdir, f"digital_{ch}.bin")
                if not os.path.exists(path):
                    raise LogicError(f"Logic 2 did not export {path}")
                trains[ch] = read_binary(path)
            return trains
        except LogicError:
            raise
        except Exception as exc:
            raise LogicError(f"capture failed: {exc}") from exc
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    @staticmethod
    def _pick_rate(allowed: List[int], wanted: int) -> int:
        """Fastest allowed rate no greater than ``wanted`` (else the slowest)."""
        under = [r for r in allowed if r <= wanted]
        return max(under) if under else min(allowed)

    def _negotiate_rate(self, wanted: int, n_channels: int) -> int:
        known = self._allowed_rates.get(n_channels)
        return self._pick_rate(known, wanted) if known else wanted

    # ----------------------------------------------------------- diagnostics
    def scan_channels(
        self, channels: List[int], duration_s: float = 0.01, sample_rate: int = 125_000_000
    ) -> Dict[int, dict]:
        """Capture several channels slowly to see which ones are alive.

        Used by the GUI's "Detect" button so the wiring does not have to be
        guessed: it reports edge count, mean period and duty per channel.
        """
        saved_rate = self.settings.sample_rate
        self.settings.sample_rate = sample_rate
        try:
            trains = self.capture(duration_s=duration_s, channels=channels)
        finally:
            self.settings.sample_rate = saved_rate

        report: Dict[int, dict] = {}
        for ch, train in trains.items():
            _, per = train.periods()
            report[ch] = {
                "edges": train.num_edges,
                "level": train.initial_state,
                "duty": train.duty(),
                "mean_period": float(per.mean()) if per.size else float("nan"),
                "freq": float(1.0 / per.mean()) if per.size and per.mean() > 0 else 0.0,
            }
        return report
