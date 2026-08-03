#!/usr/bin/env python3
"""jnwtemp command-line entry point."""

from __future__ import annotations

import click


@click.command()
@click.option("--port", default=None, help="Serial port of the TT demo board (default: autodetect)")
@click.option("--no-board/--board", default=False, help="Run Saleae-only, without the demo board")
@click.option(
    "--source",
    type=click.Choice(["board", "logic"]),
    default="board",
    show_default=True,
    help=(
        "Where the timing comes from. 'board' measures the chip with the "
        "RP2350's PIO and needs no other instrument; 'logic' uses the Saleae, "
        "which is the only way to get the per-event streams."
    ),
)
def main(port: str, no_board: bool, source: str) -> None:
    """Live temperature readout for JNW-TEMP (Tiny Tapeout project 258).

    The default source is the demo board alone: plug in USB and go. With
    ``--source logic`` it instead captures on a Saleae Logic Pro, which needs
    Logic 2 running with its automation server enabled (Preferences ->
    "Enable automation server"); the demo board is still used to drive the chip.

    The two sources get different windows, because they are different
    instruments: the Saleae one has capture length, sample rate and threshold,
    and the board one has none of those - it counts continuously.
    """
    from .app import run

    raise SystemExit(run(board_port=port, use_board=not no_board, source=source))


if __name__ == "__main__":
    main()
