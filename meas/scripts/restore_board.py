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
"""Back up and restore the demo board's filesystem.

The board's Python experiments only ever live in RAM, so the state that can
actually be lost is the filesystem: config.ini, main.py and the shuttle tables.
This makes a byte-exact copy of it and puts it back.

    ./restore_board.py backup                     # snapshot -> board-backup/<stamp>/
    ./restore_board.py verify board-backup/<stamp>   # compare, change nothing
    ./restore_board.py restore board-backup/<stamp> # write the snapshot back
    ./restore_board.py reset                      # soft reboot: clears PIO/RAM state

What this does NOT cover is the firmware itself. MicroPython and the ttboard
SDK are frozen into the .uf2 and are not files on the filesystem, so they
cannot be read back over the REPL. `manifest.json` records the exact build
(see `info`); to reproduce it, flash that release and then `restore` on top.
A byte-exact flash image needs BOOTSEL mode and `picotool save -a fw.uf2`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from jnwtemp.board import TTBoard  # noqa: E402

#: Read/write payload per REPL round trip. 1 kB keeps a chunk well inside the
#: raw REPL's comfort zone and still moves ~44 kB/s.
CHUNK = 1024

DEFAULT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "board-backup"
)


def board_files(b: TTBoard) -> list:
    """Every file on the board, as (path, size)."""
    listing = b.exec(
        "import os\n"
        "def walk(d):\n"
        "    try:\n"
        "        entries = os.listdir(d)\n"
        "    except Exception:\n"
        "        return\n"
        "    for name in entries:\n"
        "        p = (d + '/' + name) if d != '/' else '/' + name\n"
        "        try:\n"
        "            st = os.stat(p)\n"
        "        except Exception:\n"
        "            continue\n"
        "        if st[0] & 0x4000:\n"
        "            walk(p)\n"
        "        else:\n"
        "            print(p, st[6])\n"
        "walk('/')\n",
        timeout=30,
    )
    out = []
    for line in listing.splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit():
            out.append((parts[0], int(parts[1])))
    return out


def read_file(b: TTBoard, path: str, size: int) -> bytes:
    data = b""
    while len(data) < size:
        out = b.exec(
            "import ubinascii\n"
            f"f = open({path!r}, 'rb')\n"
            f"f.seek({len(data)})\n"
            f"print(ubinascii.b2a_base64(f.read({CHUNK})).decode().strip())\n"
            "f.close()\n",
            timeout=20,
        )
        blob = base64.b64decode(out.strip())
        if not blob:
            break
        data += blob
    return data


def write_file(b: TTBoard, path: str, data: bytes) -> None:
    parts = path.strip("/").split("/")[:-1]
    grown = ""
    for part in parts:
        grown += "/" + part
        b.exec(f"import os\ntry:\n    os.mkdir({grown!r})\nexcept OSError:\n    pass\n")
    for off in range(0, max(len(data), 1), CHUNK):
        chunk = data[off : off + CHUNK]
        mode = "wb" if off == 0 else "ab"
        b.exec(
            "import ubinascii\n"
            f"f = open({path!r}, {mode!r})\n"
            f"f.write(ubinascii.a2b_base64({base64.b64encode(chunk).decode()!r}))\n"
            "f.close()\n",
            timeout=20,
        )


def do_backup(b: TTBoard, root: str) -> str:
    dest = os.path.join(root, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest, exist_ok=True)
    info = b.exec(
        "import os, sys, gc, machine\n"
        "print('uname   ', os.uname())\n"
        "print('impl    ', sys.implementation)\n"
        "print('sysclk  ', machine.freq())\n"
        "print('freemem ', gc.mem_free())\n",
        timeout=10,
    )
    manifest = {
        "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        "port": b.port,
        "info": info,
        "files": {},
    }
    files = board_files(b)
    print(f"{len(files)} files, {sum(s for _, s in files)/1024:.1f} kB")
    for path, size in files:
        data = read_file(b, path, size)
        local = os.path.join(dest, path.lstrip("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(data)
        manifest["files"][path] = {
            "size_on_board": size,
            "size_saved": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        short = "" if len(data) == size else "  <-- SHORT READ"
        print(f"  {path:<44} {len(data):>7} B{short}")
    with open(os.path.join(dest, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"\nsaved to {dest}")
    return dest


def load_manifest(src: str) -> dict:
    with open(os.path.join(src, "manifest.json")) as fh:
        return json.load(fh)


def do_verify(b: TTBoard, src: str) -> int:
    manifest = load_manifest(src)
    on_board = dict(board_files(b))
    bad = 0
    for path, meta in sorted(manifest["files"].items()):
        if path not in on_board:
            print(f"  MISSING  {path}")
            bad += 1
            continue
        data = read_file(b, path, on_board[path])
        got = hashlib.sha256(data).hexdigest()
        if got != meta["sha256"]:
            print(f"  DIFFERS  {path}  ({on_board[path]} B on board, "
                  f"{meta['size_saved']} B saved)")
            bad += 1
    extra = sorted(set(on_board) - set(manifest["files"]))
    for path in extra:
        print(f"  EXTRA    {path}  (not in the snapshot; restore leaves it alone)")
    print(f"\n{len(manifest['files']) - bad}/{len(manifest['files'])} files match"
          + (f", {len(extra)} extra on board" if extra else ""))
    return bad


def do_restore(b: TTBoard, src: str) -> None:
    manifest = load_manifest(src)
    on_board = dict(board_files(b))
    for path, meta in sorted(manifest["files"].items()):
        local = os.path.join(src, path.lstrip("/"))
        with open(local, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise SystemExit(f"snapshot is corrupt: {local} does not match the manifest")
        if path in on_board and hashlib.sha256(
            read_file(b, path, on_board[path])
        ).hexdigest() == meta["sha256"]:
            print(f"  ok       {path}")
            continue
        write_file(b, path, data)
        back = read_file(b, path, len(data))
        state = "restored" if hashlib.sha256(back).hexdigest() == meta["sha256"] else "FAILED"
        print(f"  {state:<8} {path}  {len(data)} B")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("backup", "verify", "restore", "reset"))
    ap.add_argument("snapshot", nargs="?", help="backup directory (verify/restore)")
    ap.add_argument("--port", default=None, help="serial port; default auto-detect")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="where backups are written")
    args = ap.parse_args()

    if args.action in ("verify", "restore") and not args.snapshot:
        ap.error(f"{args.action} needs a snapshot directory")

    b = TTBoard(port=args.port)
    info = b.connect()
    print(f"{info.banner} on {b.port}\n{info.project}\n")
    try:
        if args.action == "backup":
            do_backup(b, args.root)
        elif args.action == "verify":
            return 1 if do_verify(b, args.snapshot) else 0
        elif args.action == "restore":
            do_restore(b, args.snapshot)
        else:
            # Ctrl-D reboots MicroPython: PIO programs, state machines, GPIO
            # window and every global from a crashed experiment go away, and
            # main.py runs again. The filesystem is untouched.
            b._ser.write(b"\x04")
            time.sleep(2.0)
            print(b._drain(2.0).strip()[-400:])
    finally:
        b.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
