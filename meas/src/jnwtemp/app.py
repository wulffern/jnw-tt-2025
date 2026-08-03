#!/usr/bin/env python3
"""Qt application bootstrap for the jnwtemp GUI."""

from __future__ import annotations

import sys
from typing import Optional


def run(
    board_port: Optional[str] = None,
    use_board: bool = True,
    source: str = "board",
) -> int:
    """Create the QApplication, show the window for ``source``, run the loop.

    The two sources get different windows rather than one window with half its
    controls greyed out: a Saleae capture has a length, a sample rate and a
    threshold, and the board's counter has none of those.
    """
    from PySide6.QtWidgets import QApplication

    from .acquire import SOURCE_BOARD

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("jnwtemp")
    if source == SOURCE_BOARD:
        from .board_window import BoardWindow as Window
    else:
        from .main_window import MainWindow as Window
    window = Window(board_port=board_port, use_board=use_board)
    window.show()
    return app.exec()
