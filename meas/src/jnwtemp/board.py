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
"""Host-side control of the Tiny Tapeout demo board over its MicroPython REPL.

This is what closes the loop: the host selects the project, sets the project
clock, and - for GR06 - generates the ResetTemp06 pulse train on ui_in[0] while
the Saleae captures the response on uo_out[2].

The board seen here is a *TinyTapeout RP2350B Core* running the ttboard SDK,
which exposes a ``tt`` DemoBoard object and a ``set_clock_hz`` helper in the
REPL globals. Note that this port needs DTR/RTS asserted after opening before
the REPL will answer, and macOS will report EBUSY while a Chrome WebSerial tab
(e.g. TT Commander) holds it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - dependency guard
    serial = None
    list_ports = None

#: Shuttle index of JNW-TEMP on ttsky25a.
PROJECT_INDEX = 258
PROJECT_NAME = "tt_um_jnw_wulffern"

#: The RP2350B PWM cannot divide the system clock by less than two, and the SDK
#: caps the system clock at 128 MHz, so this is as high as the project clock goes.
MAX_PROJECT_CLOCK_HZ = 64_000_000


class BoardError(RuntimeError):
    """Raised when the demo board cannot be reached or a command fails."""


@dataclass
class BoardInfo:
    port: str
    banner: str
    project: str
    clock_hz: int


def find_ports() -> List[str]:
    """Candidate serial ports, MicroPython/RP2 boards first."""
    if list_ports is None:
        return []
    found, rest = [], []
    for p in list_ports.comports():
        if p.device.startswith("/dev/tty."):
            continue  # prefer the callout (cu) device on macOS
        # 0x2e8a is the Raspberry Pi / RP2 vendor id used by MicroPython builds.
        if p.vid == 0x2E8A or "MicroPython" in (p.manufacturer or ""):
            found.append(p.device)
        else:
            rest.append(p.device)
    return found + rest


class TTBoard:
    """A thin, synchronous wrapper around the board's MicroPython REPL."""

    def __init__(self, port: Optional[str] = None, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._ser = None
        self._pending = ""
        self.info: Optional[BoardInfo] = None

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> BoardInfo:
        if serial is None:
            raise BoardError("pyserial is not installed (pip install pyserial)")

        port = self.port or (find_ports()[0] if find_ports() else None)
        if port is None:
            raise BoardError("no serial port found for the demo board")

        try:
            ser = serial.Serial(timeout=0.2)
            ser.port = port
            ser.baudrate = self.baudrate
            # Open with the modem lines low, then raise them: the RP2350 CDC
            # stack only starts answering once DTR/RTS are asserted.
            ser.dtr = False
            ser.rts = False
            ser.open()
        except Exception as exc:
            raise BoardError(
                f"could not open {port}: {exc}. If a Chrome tab (TT Commander) "
                f"has the board open over WebSerial, close it first."
            ) from exc

        self._ser = ser
        self.port = port
        time.sleep(0.1)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.4)

        # Ctrl-C interrupts a running script, Ctrl-B leaves the raw REPL - in
        # that order. A session that died mid-exec leaves the board running our
        # code in the raw REPL, where a leading Ctrl-B is swallowed by the
        # running script and every later connect fails with "no prompt". That
        # is a crashed app locking everyone out of its own instrument.
        banner = ""
        for attempt, pause in enumerate((0.2, 0.5)):
            for ctrl in (b"\x03", b"\x03", b"\x02", b"\r\n"):
                ser.write(ctrl)
                time.sleep(pause)
            banner = self._drain(0.8 if attempt == 0 else 1.2)
            if ">>>" in banner:
                break
        if ">>>" not in banner:
            self.disconnect()
            raise BoardError(f"no MicroPython prompt on {port} (got {banner!r})")

        project = self.exec_eval("str(tt)")
        clock = self.exec_eval("str(tt.auto_clocking_freq)")
        try:
            clock_hz = int(clock)
        except ValueError:
            clock_hz = 0
        first_line = next((l for l in banner.splitlines() if "MicroPython" in l), "MicroPython")
        self.info = BoardInfo(port=port, banner=first_line.strip(), project=project, clock_hz=clock_hz)
        return self.info

    def disconnect(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def __enter__(self) -> "TTBoard":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------------ REPL
    # pyserial's read(n) blocks until n bytes arrive or the timeout expires, so
    # a naive read(4096) costs a full timeout on every call. Everything below
    # polls in_waiting instead, which keeps a REPL round-trip in the low ms.
    def _read_now(self) -> bytes:
        n = self._ser.in_waiting
        return self._ser.read(n) if n else b""

    def _drain(self, seconds: float) -> str:
        out = b""
        end = time.time() + seconds
        while time.time() < end:
            chunk = self._read_now()
            if chunk:
                out += chunk
            else:
                time.sleep(0.005)
        return out.decode(errors="replace")

    def _read_until(self, done, timeout: float) -> bytes:
        """Accumulate bytes until ``done(buf)`` or ``timeout`` seconds elapse."""
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            chunk = self._read_now()
            if chunk:
                buf += chunk
                if done(buf):
                    return buf
            else:
                time.sleep(0.001)
        return buf

    def exec_begin(self, code: str) -> None:
        """Start ``code`` in the raw REPL without waiting for it to finish.

        The raw REPL (Ctrl-A) is used instead of pasting into the friendly REPL
        so that multi-line code is not mangled by auto-indent, and so the end of
        the output is unambiguous (MicroPython terminates it with \\x04).
        """
        if not self.connected:
            raise BoardError("not connected to the demo board")
        ser = self._ser
        ser.reset_input_buffer()
        ser.write(b"\x01")  # enter raw REPL
        # The raw REPL announces itself with "raw REPL; CTRL-B to exit" + ">".
        self._read_until(lambda b: b.endswith(b">"), timeout=1.0)
        ser.write(code.encode() + b"\x04")  # Ctrl-D executes
        ser.flush()
        self._pending = code

    def exec_end(self, timeout: float = 5.0) -> str:
        """Wait for the code started by :meth:`exec_begin` and return its output."""
        ser = self._ser
        buf = self._read_until(lambda b: b.count(b"\x04") >= 2, timeout=timeout)
        ser.write(b"\x02")  # back to the friendly REPL
        self._read_until(lambda b: b.endswith(b">>> "), timeout=0.5)
        code, self._pending = self._pending, ""

        if buf.count(b"\x04") < 2:
            raise BoardError(f"timed out after {timeout:.1f}s running:\n{code}")
        if b"OK" in buf:
            buf = buf.split(b"OK", 1)[1]
        parts = buf.split(b"\x04")
        out = parts[0].decode(errors="replace").strip()
        err = parts[1].decode(errors="replace").strip() if len(parts) > 1 else ""
        if err:
            raise BoardError(f"board raised an error:\n{err}\nwhile running:\n{code}")
        return out

    def exec(self, code: str, timeout: float = 5.0) -> str:
        """Run ``code`` on the board and return whatever it printed."""
        self.exec_begin(code)
        return self.exec_end(timeout=timeout)

    def exec_eval(self, expr: str, timeout: float = 5.0) -> str:
        return self.exec(f"print({expr})", timeout=timeout).strip()

    # ----------------------------------------------------------- TT controls
    def select_project(self, name: str = PROJECT_NAME, index: int = PROJECT_INDEX) -> str:
        """Enable the JNW-TEMP design on the mux, by name with an index fallback."""
        code = (
            "try:\n"
            f"    tt.shuttle.{name}.enable()\n"
            "except Exception:\n"
            f"    tt.shuttle.enable({index})\n"
            "print(tt)\n"
        )
        return self.exec(code, timeout=15.0).strip()

    def set_clock_hz(self, hz: int = MAX_PROJECT_CLOCK_HZ) -> int:
        """Set the project clock; returns the frequency actually achieved.

        ``set_clock_hz`` is a convenience global that main.py *may* define in the
        REPL, but it is not always there - after some boots it is simply absent,
        and depending on it meant the clock silently stayed at 0 after a power
        cycle, which leaves GR07 dead. So drive the DemoBoard API directly and
        keep the helper only as a first choice.

        The RP2350 PWM cannot divide the system clock by less than two, so the
        system clock has to be at least 2x the requested project clock.
        """
        hz = int(min(hz, MAX_PROJECT_CLOCK_HZ))
        code = (
            "import machine\n"
            f"hz = {hz}\n"
            "try:\n"
            "    set_clock_hz(hz)\n"
            "except NameError:\n"
            "    if machine.freq() < 2 * hz:\n"
            "        machine.freq(2 * hz)\n"
            "    tt.clock_project_PWM(hz)\n"
            "print(tt.auto_clocking_freq)\n"
        )
        out = self.exec(code, timeout=15.0).strip().splitlines()
        for line in reversed(out):
            try:
                return int(line.strip())
            except ValueError:
                continue
        return int(self.exec_eval("tt.auto_clocking_freq"))

    def clock_running(self) -> int:
        """Project clock frequency as the board reports it. 0 means stopped."""
        try:
            return int(self.exec_eval("tt.auto_clocking_freq"))
        except (BoardError, ValueError):
            return 0

    def stop_clock(self) -> None:
        self.exec("tt.clock_project_stop()")

    def read_uo_out(self) -> int:
        return int(self.exec_eval("int(tt.uo_out.value)"))

    def set_ui_in(self, bit: int, value: int) -> None:
        self.exec(f"tt.ui_in[{int(bit)}] = {1 if value else 0}")

    def reset_project(self) -> None:
        self.exec("tt.reset_project(True); tt.reset_project(False)")

    #: Measured per-iteration cost of the MicroPython pulse loop below, used to
    #: predict how many pulses a capture window will actually contain.
    PULSE_LOOP_OVERHEAD_US = 12.0

    def _pulse_code(self, bit: int, count: int, high_us: int, low_us: int) -> str:
        # tt.ui_in[bit] = v goes through the SDK's Logic wrapper, which costs
        # ~15 ms per write; the underlying machine.Pin costs ~6 us. At tens of
        # microseconds per ResetTemp06 pulse only the raw pin is usable.
        return (
            "import time\n"
            f"p = tt.pins.ui_in{int(bit)}.raw_pin\n"
            f"for _ in range({int(count)}):\n"
            "    p.value(1)\n"
            f"    time.sleep_us({int(high_us)})\n"
            "    p.value(0)\n"
            f"    time.sleep_us({int(low_us)})\n"
            "p.value(0)\n"
        )

    def pulse_budget_s(self, count: int, high_us: int, low_us: int) -> float:
        """Wall-clock the board needs for :meth:`pulse_ui_in` with these settings."""
        return count * (high_us + low_us + self.PULSE_LOOP_OVERHEAD_US) / 1e6

    def pulse_ui_in(self, bit: int, count: int, high_us: int, low_us: int) -> None:
        """Emit a burst of ``count`` pulses on ui_in[bit], on the board itself."""
        self.pulse_ui_in_begin(bit, count, high_us, low_us)
        self.pulse_ui_in_end(count, high_us, low_us)

    def pulse_ui_in_begin(self, bit: int, count: int, high_us: int, low_us: int) -> None:
        """Start the pulse burst and return immediately.

        The burst has to run on the RP2350 rather than as host round-trips: a
        REPL round-trip is milliseconds, whereas GR06 wants ResetTemp06 asserted
        for tens of microseconds. Starting it asynchronously lets the Saleae
        capture run concurrently, so one burst fills one capture window with
        hundreds of independent temperature measurements.
        """
        self.exec_begin(self._pulse_code(bit, count, high_us, low_us))

    def pulse_ui_in_end(self, count: int, high_us: int, low_us: int) -> None:
        self.exec_end(timeout=self.pulse_budget_s(count, high_us, low_us) + 10.0)
