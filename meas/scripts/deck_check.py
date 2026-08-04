#!/usr/bin/env python3
"""Execute the presentation's JavaScript and check every chart actually draws.

The deck draws all its charts in one <script>, each in its own IIFE with no
try/catch, so the first one that throws kills every chart after it. That is
exactly what happened: a series carrying an instrument floor had no `pts`, the
noise chart threw on `p[0]`, and the published page lost most of its figures.
Nothing caught it because "it built" and "the ids resolve" were the only checks
being run, and neither executes a line of the code.

There is no node on this machine, but macOS ships JavaScriptCore via
`osascript -l JavaScript`, which is a real engine. This stubs out just enough
DOM for the chart code to run, executes the deck's script, and then asserts
that each chart element received children - because "no exception" is not the
same as "something was drawn".

    python3 scripts/deck_check.py [presentation.html]

Exits non-zero on a script error or on a chart that drew nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DECK = os.path.join(os.path.dirname(HERE), "docs", "presentation.html")

#: Charts allowed to draw nothing, with the reason. Anything else drawing
#: nothing is a failure - an empty chart on a slide is indistinguishable from a
#: broken one to a reader.
ALLOW_EMPTY = {
    # the per-period Saleae capture it needs was pruned from the repo; the
    # chart renders an explanatory note instead, which is a text child, so it
    # should NOT be empty. Kept here only if that note is ever removed.
}

DOM_STUB = r"""
var REAL_IDS = %(ids)s;
function mkEl(tag, id) {
  return {
    tagName: tag, id: id || "", children: [], style: {}, dataset: {},
    textContent: "", innerHTML: "", _attrs: {},
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    getAttribute: function (k) {
      if (k === "viewBox" && !(k in this._attrs)) return "0 0 980 340";
      return (k in this._attrs) ? this._attrs[k] : null;
    },
    appendChild: function (c) { this.children.push(c); return c; },
    removeChild: function (c) { return c; },
    insertBefore: function (c) { this.children.push(c); return c; },
    addEventListener: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    getBoundingClientRect: function () {
      return { x: 0, y: 0, width: 980, height: 340, top: 0, left: 0 }; },
    classList: { add: function () {}, remove: function () {},
                 toggle: function () {}, contains: function () { return false; } },
    insertAdjacentHTML: function () {},
    closest: function () { return null; },
    focus: function () {}, scrollIntoView: function () {}, click: function () {}
  };
}
var document = {
  _byId: {},
  getElementById: function (id) {
    if (REAL_IDS.indexOf(id) === -1) return null;   // faithful: absent -> null
    if (!this._byId[id]) this._byId[id] = mkEl("div", id);
    return this._byId[id];
  },
  createElement: function (t) { return mkEl(t); },
  createElementNS: function (ns, t) { return mkEl(t); },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  body: mkEl("body"), documentElement: mkEl("html")
};
var window = {
  addEventListener: function () {},
  matchMedia: function () { return { matches: false,
                                     addEventListener: function () {} }; },
  requestAnimationFrame: function () { return 0; },
  getComputedStyle: function () {
    return { getPropertyValue: function () { return ""; } }; },
  location: { hash: "", href: "" }, innerWidth: 1400, innerHeight: 900
};
var navigator = { userAgent: "jsc" };
var console = { log: function () {}, warn: function () {},
                error: function () {} };
"""

REPORT = r"""
var _out = { charts: {}, filled: 0, blank: [] };
for (var k in document._byId) {
  var e = document._byId[k];
  if (k.indexOf("chart") === 0) _out.charts[k] = e.children.length;
  if (e.textContent !== "" || e.innerHTML !== "") _out.filled++;
}
JSON.stringify(_out);
"""


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DECK
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found - run build_presentation.py first")
    html = open(path, encoding="utf-8").read()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    ids = sorted(set(re.findall(r'id="([^"]+)"', html)))
    if not scripts:
        raise SystemExit("no <script> block in the deck")

    body = "\n;\n".join(scripts)
    prog = (DOM_STUB % {"ids": json.dumps(ids)}
            # the deck defines `const $`, which collides with JXA's ObjC bridge
            # global, so give it its own scope
            + "try { (function(){\n" + body + "\n})(); }\n"
            + "catch (e) { throw new Error('DECK ERROR: ' + e.message + "
              "'\\nstack: ' + (e.stack || '')); }\n"
            + REPORT)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(prog)
        tmp = fh.name
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", tmp],
                           capture_output=True, text=True)
    finally:
        os.unlink(tmp)

    if r.returncode != 0:
        print(r.stderr.strip().replace("\\n", "\n"))
        print("\nFAIL - the deck's script raised; every chart after the throw "
              "is blank on the published page")
        return 1

    try:
        out = json.loads(r.stdout.strip())
    except ValueError:
        print("could not parse the report:", r.stdout[:400])
        return 1

    charts = out["charts"]
    print(f"{len(scripts)} script block(s), {len(ids)} ids, "
          f"{out['filled']} elements written to\n")
    bad = []
    for name in sorted(charts):
        n = charts[name]
        flag = ""
        if n == 0 and name not in ALLOW_EMPTY:
            flag = "   <-- DREW NOTHING"
            bad.append(name)
        print(f"  {name:16} {n:5d} elements{flag}")

    if bad:
        print(f"\nFAIL - {len(bad)} chart(s) drew nothing: {', '.join(bad)}")
        return 1
    print("\nPASS - script runs clean and every chart drew")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
