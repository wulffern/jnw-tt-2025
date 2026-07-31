#!/usr/bin/env python3
"""Qt application bootstrap for the jnwtemp GUI."""

from __future__ import annotations

import sys
from typing import Optional


def run(board_port: Optional[str] = None, use_board: bool = True) -> int:
    """Create the QApplication, show the main window, and run the event loop."""
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("jnwtemp")
    window = MainWindow(board_port=board_port, use_board=use_board)
    window.show()
    return app.exec()
