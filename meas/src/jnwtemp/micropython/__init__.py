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
"""MicroPython snippets that run on the TT demo board, not on the host.

Everything in this directory is source for the *RP2350*. It is typed into the
board's raw REPL over USB CDC and executed there, in a namespace where ``tt``
(the ``ttboard`` DemoBoard) already exists. None of it is importable here, and
none of it can use anything from this package.

They are kept as files rather than as string literals inside :mod:`jnwtemp.board`
for three reasons: the board-side code is a different language runtime and reads
as one when it is not wrapped in ``"...\\n"`` per line; the comments explaining
*why* each snippet is shaped the way it is belong next to the code they explain;
and a snippet can be pasted straight into a REPL to debug it by hand, which is
how most of them were arrived at.

The ``.upy`` extension is deliberate. These are not valid host Python - the
``$name`` placeholders below are substitution points, not syntax - so a ``.py``
extension would put files in the tree that every linter and import scanner would
try and fail to read.

Substitution is :class:`string.Template`, i.e. ``$name`` and ``${name}``, chosen
over ``str.format`` because MicroPython code is full of braces and empty of
dollar signs. Substitution is strict: a missing or misspelled key raises rather
than silently emitting a literal ``$hz`` for the board to choke on.
"""
from __future__ import annotations

import os
from string import Template
from typing import Dict

HERE = os.path.dirname(os.path.abspath(__file__))
SUFFIX = ".upy"

_cache: Dict[str, Template] = {}


def path(name: str) -> str:
    """Absolute path of a snippet, without loading it."""
    return os.path.join(HERE, name + SUFFIX)


def load(name: str) -> Template:
    """The named snippet as a :class:`string.Template`, cached."""
    if name not in _cache:
        p = path(name)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                _cache[name] = Template(fh.read())
        except OSError as exc:
            raise FileNotFoundError(
                f"MicroPython snippet {name!r} not found at {p}. It ships as "
                f"package data; an installed copy missing it means the wheel "
                f"was built without {SUFFIX} files."
            ) from exc
    return _cache[name]


def render(_name: str, /, **values) -> str:
    """Fill a snippet's placeholders and return code ready for the REPL.

    Uses ``substitute``, not ``safe_substitute``: a typo in a key name should
    fail here, on the host, rather than reaching the board as a stray ``$hz``
    that raises a confusing NameError three layers away.

    The snippet name is positional-only because ``select_project.upy`` has a
    ``$name`` placeholder of its own, and a keyword parameter called ``name``
    would collide with it.
    """
    return load(_name).substitute(**values)


def available() -> list:
    """Snippet names present on disk, for diagnostics."""
    return sorted(
        f[: -len(SUFFIX)] for f in os.listdir(HERE) if f.endswith(SUFFIX)
    )
