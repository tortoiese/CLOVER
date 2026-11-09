"""Keep one QApplication alive across the full cross-platform Qt test session."""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def qt_application() -> Iterator[QApplication]:
    """Prevent temporary QApplication wrappers from destroying the native singleton."""
    app = QApplication.instance() or QApplication([])
    yield app
    app.closeAllWindows()
    app.processEvents()
