#!/usr/bin/env python3
"""Probe a Vötsch VT chamber over its ASCII-2 TCP interface.

Bench aid for the case where the GUI says the chamber failed: it tries the
socket, sends a raw interrogation, and prints exactly what comes back so the
field layout can be read off rather than guessed.

    python scripts/chamber_probe.py [host] [port] [address]
"""

from __future__ import annotations

import socket
import sys
import time


def probe(host: str, port: int, address: int) -> int:
    print(f"--- connecting to {host}:{port} (address {address:02d})")
    try:
        sock = socket.create_connection((host, port), timeout=5.0)
    except OSError as exc:
        print(f"TCP CONNECT FAILED: {type(exc).__name__}: {exc}")
        return 1
    print("TCP CONNECT OK")
    sock.settimeout(5.0)

    for cmd in (f"${address:02d}I\r", f"${address:02d}?\r"):
        print(f"\n--- sending {cmd!r}")
        try:
            sock.sendall(cmd.encode("latin-1"))
        except OSError as exc:
            print(f"SEND FAILED: {exc}")
            break
        buf = bytearray()
        end = time.time() + 5.0
        while time.time() < end:
            try:
                chunk = sock.recv(256)
            except socket.timeout:
                print("(recv timed out)")
                break
            except OSError as exc:
                print(f"RECV FAILED: {exc}")
                break
            if not chunk:
                print("(peer closed the connection)")
                break
            buf.extend(chunk)
            if b"\r" in chunk or b"\n" in chunk:
                break
        print(f"reply {len(buf)} bytes: {bytes(buf)!r}")
        if buf:
            text = buf.decode("latin-1").strip("\r\n")
            tokens = text.split()
            print(f"  tokens ({len(tokens)}):")
            for i, tok in enumerate(tokens):
                kind = "bits" if set(tok) <= {"0", "1"} and len(tok) >= 4 else "value"
                print(f"    [{i:2d}] {kind:5s} {tok}")

    sock.close()
    return 0


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.17.52"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2049
    addr = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    raise SystemExit(probe(host, port, addr))
