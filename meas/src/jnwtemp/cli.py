#!/usr/bin/env python3
"""jnwtemp command-line entry point."""

from __future__ import annotations

import sys

import click


@click.group(invoke_without_command=True)
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
@click.pass_context
def main(ctx: click.Context, port: str, no_board: bool, source: str) -> None:
    """Live temperature readout for JNW-TEMP (Tiny Tapeout project 258).

    With no subcommand this opens the GUI, which is what it has always done;
    the options above belong to it. The default source is the demo board alone:
    plug in USB and go. With ``--source logic`` it instead captures on a Saleae
    Logic Pro, which needs Logic 2 running with its automation server enabled
    (Preferences -> "Enable automation server"); the demo board is still used
    to drive the chip.

    The two sources get different windows, because they are different
    instruments: the Saleae one has capture length, sample rate and threshold,
    and the board one has none of those - it counts continuously.
    """
    # Help texts and the chamber's own status strings use '°' and '→', which a
    # cp1252 console mangles. Done here so it covers every subcommand.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    if ctx.invoked_subcommand is not None:
        return
    from .app import run

    raise SystemExit(run(board_port=port, use_board=not no_board, source=source))


@main.command()
@click.option("--out", default=None, type=click.Path(file_okay=False),
              help="Output folder  [default: data/sweep-<timestamp>]")
@click.option("--start", default=20.0, show_default=True, help="First setpoint, °C")
@click.option("--stop", default=30.0, show_default=True, help="Last setpoint, °C (inclusive)")
@click.option("--step", default=5.0, show_default=True, help="Step size, °C")
@click.option("--tol", default=0.3, show_default=True,
              help="Stabilisation band around the setpoint, °C")
@click.option("--soak", default=120.0, show_default=True,
              help="Time inside the band before the plateau starts, s")
@click.option("--dwell", default=300.0, show_default=True,
              help="Length of the measurement plateau, s")
@click.option("--max-settle", default=1800.0, show_default=True,
              help="Give up on a point that will not stabilise within, s")
@click.option("--window", default=1.0, show_default=True,
              help="Seconds of PIO counting per reading")
@click.option("--bin-ms", default=1.0, show_default=True,
              help="Sub-window bins; 0 makes each reading one measurement")
@click.option("--period", default=5.0, show_default=True,
              help="Target time between readings, s")
@click.option("--leave-on", is_flag=True,
              help="Leave the chamber running at the last setpoint")
@click.option("--board-port", default=None, help="Serial port of the demo board")
@click.option("--chamber-host", default=None, help="Chamber address  [default: the bench unit]")
def sweep(out, start, stop, step, tol, soak, dwell, max_settle, window,
          bin_ms, period, leave_on, board_port, chamber_host) -> None:
    """Step the chamber through a temperature range, logging both sensors.

    Board only - no Saleae. Each setpoint is commanded, allowed to stabilise
    inside --tol for --soak, and then measured for --dwell. Rows taken while
    settling are kept too but tagged 'settling' in the phase column, so a
    calibration fit can select only the plateaus.

    Writes sweep.csv and sweep.meta.json; the sidecar carries every setting
    above plus the git commit, and is written before the run as well as after.

    Close the GUI first: it holds the demo board's serial port, and two
    processes cannot share it.
    """
    from .sweep import SweepConfig, run_sweep

    cfg = SweepConfig(
        start_c=start, stop_c=stop, step_c=step, tol_c=tol, soak_s=soak,
        dwell_s=dwell, max_settle_s=max_settle, window_s=window, bin_ms=bin_ms,
        period_s=period, leave_on=leave_on, board_port=board_port,
    )
    if chamber_host:
        cfg.chamber_host = chamber_host
    run_sweep(cfg, out_dir=out, log=click.echo)


if __name__ == "__main__":
    main()
