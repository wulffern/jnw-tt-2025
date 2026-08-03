#!/usr/bin/env python3
"""Explore the S!MPAC (c3k) web interface on the chamber.

Read-only: GETs a list of candidate pages and prints status, size and any
title/heading, to locate the electronic type plate (which names the model) and
the interface/operating-mode settings.

    python scripts/chamber_web.py [host]
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

CANDIDATES = [
    "/",
    "/simpac/index.plc",
    "/simpac/main.plc",
    "/simpac/menu.plc",
    "/simpac/frame.plc",
    "/simpac/start.plc",
    "/simpac/status.plc",
    "/simpac/values.plc",
    "/simpac/set/setupvalues_en.plc",
    "/simpac/set/setup_en.plc",
    "/simpac/set/index.plc",
    "/simpac/typeplate.plc",
    "/simpac/typenschild.plc",
    "/typeplate.html",
    "/index.html",
    "/simpac/info.plc",
    "/simpac/interface.plc",
    "/simpac/ethernet.plc",
]


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.read().decode("iso-8859-1", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001 - report and continue
        return None, str(exc)


def main(host: str) -> int:
    for path in CANDIDATES:
        url = f"http://{host}{path}"
        status, body = get(url)
        if status is None:
            print(f"{path:36s} ERROR {body[:60]}")
            continue
        title = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
        heads = re.findall(r"<h\d[^>]*>(.*?)</h\d>", body, re.I | re.S)
        note = title.group(1).strip() if title else ""
        if heads:
            note += " | " + " / ".join(re.sub(r"<[^>]+>", "", h).strip() for h in heads[:4])
        print(f"{path:36s} {status} {len(body):6d}  {note[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "192.168.17.52"))
