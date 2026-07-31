#!/usr/bin/env python3
"""Convenience launcher: run the jnwtemp GUI without installing the package."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from jnwtemp.app import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
