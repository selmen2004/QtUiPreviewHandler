"""Tripwire module for the custom-widget safety test.

A .ui that declares <header>evil_widget</header> makes PyQt6's uic.loadUi emit
`from evil_widget import EvilWidget` and exec it, which would run this file's
module body. QUiLoader must never import it. If PWNED.txt appears, the renderer
is executing file-derived code and is unsafe for a shell preview handler.
"""

import os

_marker = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "build", "render-test", "PWNED.txt",
)
os.makedirs(os.path.dirname(_marker), exist_ok=True)
with open(_marker, "w", encoding="utf-8") as _fh:
    _fh.write("arbitrary code executed from a .ui customwidget header\n")

from PySide6 import QtWidgets  # noqa: E402


class EvilWidget(QtWidgets.QWidget):
    pass