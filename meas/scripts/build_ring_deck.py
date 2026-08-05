#!/usr/bin/env python3
"""Build a standalone per-design deck.

The look is not copied: the stylesheet and the small SVG helpers are lifted out
of docs/presentation.template.html at build time, so the per-design decks stay
in step with the main one instead of drifting into a stale duplicate.

    python3 scripts/ring_measure.py     # measure -> data/ring_data.json
    python3 scripts/build_ring_deck.py  # -> docs/ring-oscillator.html
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAIN = os.path.join(ROOT, "docs", "presentation.template.html")

#: deck name -> (body template, measured data, output page)
DECKS = {
    "ring": ("ring-oscillator.body.html", "ring_data.json",
             "ring-oscillator.html"),
    "pll778": ("pll778.body.html", "pll778_data.json", "pll778.html"),
}


def borrow(html, start, end, what):
    i = html.find(start)
    j = html.find(end, i + len(start))
    if i < 0 or j < 0:
        raise SystemExit(f"could not lift {what} out of the main template")
    return html[i:j + len(end)]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", nargs="?", default="ring", choices=sorted(DECKS))
    args = ap.parse_args()
    body_name, data_name, out_name = DECKS[args.deck]
    global BODY, DATA, OUT
    BODY = os.path.join(ROOT, "docs", body_name)
    DATA = os.path.join(ROOT, "data", data_name)
    OUT = os.path.join(ROOT, "docs", out_name)
    if not os.path.exists(DATA):
        raise SystemExit(f"{data_name} not found - run the measurement first")
    main_html = open(MAIN, encoding="utf-8").read()
    style = borrow(main_html, "<style>", "</style>", "the stylesheet")
    # the generic helpers: element creation, formatting, and the log-log frame
    helpers = borrow(main_html, "/* ---------- helpers ---------- */",
                     "/* ---------- fill the numbers ---------- */", "helpers")
    loglog = borrow(main_html, "/* ---------- log-log frame",
                    "/* ---------- chart 10", "the log-log chart")
    body = open(BODY, encoding="utf-8").read()
    data = json.load(open(DATA))

    html = body.replace("__STYLE__", style)
    html = html.replace("__HELPERS__", helpers.replace(
        "/* ---------- fill the numbers ---------- */", ""))
    html = html.replace("__LOGLOG__", loglog.replace("/* ---------- chart 10", ""))
    html = html.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    if "__" in re.sub(r"__[A-Z]+__", "", html):
        pass
    for token in ("__STYLE__", "__HELPERS__", "__LOGLOG__", "__DATA__"):
        if token in html:
            raise SystemExit(f"{token} was not substituted")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"wrote {os.path.relpath(OUT, ROOT)} ({len(html)/1024:.0f} kB)")


if __name__ == "__main__":
    main()
