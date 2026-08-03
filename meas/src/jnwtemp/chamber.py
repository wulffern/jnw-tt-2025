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
"""Host-side control of the Vötsch VT temperature chamber.

The chamber speaks the classic Vötsch *ASCII-2* protocol, reached over a raw
TCP socket. What is on the bench was established by probing it (see
``scripts/chamber_probe.py`` and friends) rather than assumed, because two of
the obvious defaults are wrong for this unit:

* It answers on **address 01**, not the usual 00. ``$00I`` is met with silence.
* Its status reply carries **three** value fields, not the seven of a VT 4002::

      $01I  ->  '0025.0 0025.1 0000.0 01000000000000000000000000000000'
                 setpoint actual  spare  32-bit digital word, run bit at index 1

  Field 0 is the setpoint and field 1 the measured temperature - confirmed by
  watching field 1 drift while field 0 stayed put.

* **The set command is not shaped like the status reply.** Asked to describe
  itself with ``$01?`` the gateway answers ``$01E CV01 SV01 DO00 ... DO09`` -
  two values and ten bits against the reply's three and thirty-two.

  Framing the E string like the I string is not simply rejected, which is what
  made this expensive to find: the setpoint still moves, and the chamber still
  *stops*, so most of the driver looks healthy. Only the run bit is lost, so
  the chamber can never be started. The likely reading is that the surplus
  third value ``0000.0`` is taken for the digital word, whose second character
  is a '0' - a stop, whatever the real bit field at the end says.

In front of the chamber sits a tag-based gateway. It is *silent* on a
successful write and only answers when a tag is unknown ("WRITE failed: tag
..."), so a set command cannot be confirmed from its reply; the setpoint has to
be read back instead. :meth:`VotschChamber.set_temp` does exactly that and
raises if the value did not take, which is the symptom of a chamber whose panel
is not in external/remote mode - in that state it reports happily and ignores
every write.

Kept free of Qt so it can be scripted from a notebook as well as driven by the
GUI thread.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

#: The chamber on the bench, the port its ASCII-2 gateway listens on, and the
#: address it actually answers to.
DEFAULT_HOST = "192.168.17.52"
DEFAULT_PORT = 2049
DEFAULT_ADDRESS = 1

#: Digital-word index of the run/condition bit, in both the reply and the set
#: command. Raising it starts the chamber; clearing it stops it.
RUN_BIT = 1

#: Layout of the status reply when one has not been read yet.
FALLBACK_VALUES = 3
FALLBACK_BITS = 32

#: Layout of the *set* command, which is shorter than the status reply:
#: ``$01E CV01 SV01 DO00..DO09``. Sending the reply's 3 values and 32 bits
#: instead still moves the setpoint, but the run bit is then never raised and
#: the chamber cannot be started - see the module docstring.
WRITE_VALUES = 2
WRITE_BITS = 10

#: How closely the read-back setpoint must match what we asked for, and how
#: long to let the gateway catch up before calling a write ignored.
SETPOINT_EPS_C = 0.1
WRITE_VERIFY_S = 3.0

#: One nominal value field: ``0080.0`` or ``-070.0`` - six characters, one
#: decimal, zero padded.
_FIELD = "{:06.1f}"

REMOTE_MODE_HINT = (
    "the chamber accepted the command but did not change its setpoint. "
    "Its panel is almost certainly not in external/remote mode "
    "(Betriebsart: extern) - in manual mode it answers status requests and "
    "silently ignores every write."
)


class ChamberError(RuntimeError):
    """Raised when the chamber cannot be reached or refuses a command."""


@dataclass
class ChamberStatus:
    """One interrogation of the chamber."""

    setpoint_c: float
    actual_c: float
    running: bool
    bits: str = ""
    values: List[float] = field(default_factory=list)
    raw: str = ""

    def describe(self) -> str:
        state = "running" if self.running else "idle"
        return f"{self.actual_c:.1f} °C → set {self.setpoint_c:.1f} °C ({state})"


class VotschChamber:
    """A thin, synchronous ASCII-2 client for the VT chamber over TCP."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        address: int = DEFAULT_ADDRESS,
        timeout: float = 4.0,
    ) -> None:
        self.host = host
        self.port = port
        self.address = int(address)
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        #: (value fields, digital bits) learned from the last status, so a set
        #: command is framed the way this particular chamber reports itself
        #: rather than the way one model happens to be documented.
        self._layout: Tuple[int, int] = (FALLBACK_VALUES, FALLBACK_BITS)

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> ChamberStatus:
        """Open the socket and prove the link with one interrogation."""
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
            self._sock.settimeout(self.timeout)
        except OSError as exc:
            self._sock = None
            raise ChamberError(
                f"could not reach the chamber at {self.host}:{self.port}: {exc}"
            ) from exc
        try:
            return self.status()
        except ChamberError:
            self.close()
            raise

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def __enter__(self) -> "VotschChamber":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -------------------------------------------------------------- protocol
    def _txn(self, command: str, expect_reply: bool = True) -> str:
        """Send one command and read the reply up to the carriage return.

        ``expect_reply`` is False for writes: this gateway says nothing at all
        when a write is taken, so silence there is success, not a fault.
        """
        if self._sock is None:
            raise ChamberError("not connected to the chamber")
        try:
            self._sock.sendall(command.encode("latin-1"))
        except OSError as exc:
            raise ChamberError(f"send failed: {exc}") from exc

        buf = bytearray()
        end = time.time() + self.timeout
        while time.time() < end:
            try:
                chunk = self._sock.recv(256)
            except socket.timeout:
                break
            except OSError as exc:
                raise ChamberError(f"receive failed: {exc}") from exc
            if not chunk:
                break
            buf.extend(chunk)
            if b"\r" in chunk or b"\n" in chunk:
                break
        text = buf.decode("latin-1").strip("\r\n")
        if not buf and expect_reply:
            raise ChamberError(
                f"no reply to {command.strip()!r} from {self.host}:{self.port} "
                f"(address {self.address:02d}) - wrong address? this chamber "
                f"answers on 01, not 00"
            )
        if "failed" in text.lower():
            raise ChamberError(f"gateway rejected {command.strip()!r}: {text}")
        return text

    def _prefix(self) -> str:
        return f"${self.address:02d}"

    def status(self) -> ChamberStatus:
        """Interrogate the chamber and parse its reply."""
        status = self._parse_status(self._txn(f"{self._prefix()}I\r"))
        self._layout = (len(status.values), len(status.bits) or FALLBACK_BITS)
        return status

    @staticmethod
    def _parse_status(reply: str) -> ChamberStatus:
        values: List[float] = []
        bits = ""
        for tok in reply.split():
            # The digital word is the one all-binary token; everything else is
            # a measurement channel.
            if set(tok) <= {"0", "1"} and len(tok) >= 4:
                bits = tok
            else:
                try:
                    values.append(float(tok))
                except ValueError:
                    continue
        if len(values) < 2:
            raise ChamberError(f"could not parse chamber reply: {reply!r}")
        running = len(bits) > RUN_BIT and bits[RUN_BIT] == "1"
        return ChamberStatus(
            setpoint_c=values[0],
            actual_c=values[1],
            running=running,
            bits=bits,
            values=values,
            raw=reply,
        )

    def _set_command(self, temp_c: float, on: bool) -> str:
        """Build a ``$xxE`` set string in the chamber's own write layout.

        Deliberately not the layout of the status reply: the E string is the
        shorter of the two, and framing it like the I string costs the run bit.
        """
        n_values, n_bits = WRITE_VALUES, WRITE_BITS
        nominal = " ".join(
            [_FIELD.format(temp_c)]
            + [_FIELD.format(0.0)] * max(0, n_values - 1)
        )
        bits = ["0"] * n_bits
        bits[RUN_BIT] = "1" if on else "0"
        return f"{self._prefix()}E {nominal} {''.join(bits)}\r"

    def set_temp(self, temp_c: float, on: bool = True) -> ChamberStatus:
        """Set the nominal temperature and (by default) start the chamber.

        Raises if the chamber did not take the command. The gateway is silent
        on success, so the only honest confirmation is to read the state back -
        both the setpoint and the run bit, since a chamber that ignores writes
        ignores the stop command just as quietly as the setpoint.
        """
        self._txn(self._set_command(temp_c, on), expect_reply=False)

        def taken(s: ChamberStatus) -> bool:
            return abs(s.setpoint_c - temp_c) <= SETPOINT_EPS_C and s.running == on

        deadline = time.time() + WRITE_VERIFY_S
        status = self.status()
        while not taken(status) and time.time() < deadline:
            time.sleep(0.3)
            status = self.status()
        if not taken(status):
            wanted = f"{temp_c:.1f} °C, {'running' if on else 'off'}"
            got = f"{status.setpoint_c:.1f} °C, {'running' if status.running else 'off'}"
            raise ChamberError(
                f"chamber did not take the command (asked for {wanted}, still "
                f"{got}): {REMOTE_MODE_HINT}"
            )
        return status

    def start(self, temp_c: Optional[float] = None) -> ChamberStatus:
        """Start the chamber, optionally at a new setpoint."""
        if temp_c is None:
            temp_c = self.status().setpoint_c
        return self.set_temp(temp_c, on=True)

    def stop(self) -> ChamberStatus:
        """Hold the current setpoint but switch the chamber off."""
        return self.set_temp(self.status().setpoint_c, on=False)
