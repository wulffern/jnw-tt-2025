#!/usr/bin/env python3
"""Build the standalone presentation by injecting measured data into the template.

    python3 scripts/analyse_dual.py       # data/*.csv -> data/deck_data.json
    python3 scripts/build_presentation.py  # + the template -> docs/presentation.html

docs/presentation.template.html keeps a __DATA__ placeholder so the layout stays
readable and diffable; docs/presentation.html is the built, self-contained result
and is the file committed and published to GitHub Pages. Every number in the deck
comes from the run rather than being typed in, so the two cannot drift apart.
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DATA = os.path.join(ROOT, "data", "deck_data.json")
TEMPLATE = os.path.join(ROOT, "docs", "presentation.template.html")
OUT = os.path.join(ROOT, "docs", "presentation.html")

with open(DATA) as fh:
    d = json.load(fh)
d["stamp"] = time.strftime("%Y-%m-%d %H:%M %Z")

# The stimulus run (freeze spray + finger) is reduced separately by
# build_event_data.py; fold it in if it has been generated.
EVENTS = os.path.join(ROOT, "data", "event_data.json")
if os.path.exists(EVENTS):
    with open(EVENTS) as fh:
        d["events"] = json.load(fh)
else:
    print(f"note: {EVENTS} missing - the stimulus slides will be empty")

with open(TEMPLATE) as fh:
    html = fh.read()
if "__DATA__" not in html:
    raise SystemExit(f"{TEMPLATE} has no __DATA__ placeholder")
html = html.replace("__DATA__", json.dumps(d, separators=(",", ":")))

with open(OUT, "w") as fh:
    fh.write(html)
print(f"wrote {OUT} ({os.path.getsize(OUT)/1024:.0f} kB)")
