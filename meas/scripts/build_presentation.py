#!/usr/bin/env python3
"""Build the standalone presentation by injecting measured data into the template.

    python3 scripts/analyse_dual.py      # data/*.csv  -> data/deck_data.json
    python3 scripts/build_presentation.py # + docs/presentation.html -> presentation-built.html

The template keeps a __DATA__ placeholder so it stays readable and diffable; every
number in the deck comes from the run rather than being typed in.
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DATA = os.path.join(ROOT, "data", "deck_data.json")
TEMPLATE = os.path.join(ROOT, "docs", "presentation.html")
OUT = os.path.join(ROOT, "docs", "presentation-built.html")

with open(DATA) as fh:
    d = json.load(fh)
d["stamp"] = time.strftime("%Y-%m-%d %H:%M %Z")

with open(TEMPLATE) as fh:
    html = fh.read()
if "__DATA__" not in html:
    raise SystemExit(f"{TEMPLATE} has no __DATA__ placeholder")
html = html.replace("__DATA__", json.dumps(d, separators=(",", ":")))

with open(OUT, "w") as fh:
    fh.write(html)
print(f"wrote {OUT} ({os.path.getsize(OUT)/1024:.0f} kB)")
