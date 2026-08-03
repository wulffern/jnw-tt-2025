#!/usr/bin/env python3
"""Find how the Vötsch chamber actually wants to be talked to.

Two passes: which TCP ports are open, then - on each open port - which
ASCII-2 address and line ending produces a reply. Print-only, changes nothing
on the chamber.

    python scripts/chamber_scan.py [host]
"""

from __future__ import annotations

import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

#: Ports worth trying: the Vötsch/Simserv default, the usual serial-to-Ethernet
#: gateway ports (Lantronix, Moxa), Modbus/TCP, telnet and a few HTTP-ish ones.
CANDIDATE_PORTS = [
    23, 80, 502, 1001, 2000, 2049, 2101, 2701, 3001, 4001, 4321,
    5000, 5001, 5025, 7777, 8000, 8080, 9100, 10001, 10002, 30718,
]


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def try_command(host: str, port: int, cmd: str, wait: float = 2.5):
    """Send one command, return the raw reply (or None)."""
    try:
        sock = socket.create_connection((host, port), timeout=3.0)
    except OSError:
        return None
    sock.settimeout(wait)
    try:
        sock.sendall(cmd.encode("latin-1"))
        buf = bytearray()
        end = time.time() + wait
        while time.time() < end:
            try:
                chunk = sock.recv(256)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if b"\r" in chunk or b"\n" in chunk:
                break
        return bytes(buf)
    finally:
        sock.close()


def main(host: str) -> int:
    print(f"=== scanning {host} for open TCP ports")
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda p: (p, port_open(host, p)), CANDIDATE_PORTS))
    open_ports = [p for p, ok in results if ok]
    print(f"open: {open_ports or 'none'}")
    if not open_ports:
        print("Nothing answered. Check the IP, the cable and any firewall.")
        return 1

    print("\n=== probing ASCII-2 on each open port")
    for port in open_ports:
        for addr in (0, 1, 2):
            for ending, label in ((b"\r", "CR"), (b"\r\n", "CRLF"), (b"", "none")):
                cmd = f"${addr:02d}I" + ending.decode("latin-1")
                reply = try_command(host, port, cmd)
                tag = f"port {port:5d}  addr {addr:02d}  end {label:4s}"
                if reply:
                    print(f"{tag} -> REPLY {len(reply)} bytes: {reply!r}")
                else:
                    print(f"{tag} -> (silence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "192.168.17.52"))
