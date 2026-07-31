#!/usr/bin/env python3
"""jnwtemp command-line entry point."""

from __future__ import annotations

import click


@click.command()
@click.option("--port", default=None, help="Serial port of the TT demo board (default: autodetect)")
@click.option("--no-board/--board", default=False, help="Run Saleae-only, without the demo board")
def main(port: str, no_board: bool) -> None:
    """Live temperature readout for JNW-TEMP (Tiny Tapeout project 258).

    Needs Logic 2 running with its automation server enabled (Preferences ->
    "Enable automation server") and, for the closed loop, the Tiny Tapeout demo
    board on USB with no other program holding its serial port.
    """
    from .app import run

    raise SystemExit(run(board_port=port, use_board=not no_board))


if __name__ == "__main__":
    main()
