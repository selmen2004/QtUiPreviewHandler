"""Standalone tests for the .ui rendering core (no COM, no Explorer).

Run:  .venv\\Scripts\\python.exe tests\\test_render.py
Writes PNGs to build/render-test/ so they can be eyeballed.
"""

from __future__ import annotations

import os
import sys
import time
import hashlib
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "renderer"))

import ui_render  # noqa: E402
import PySide6  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples")
OUT = os.path.join(ROOT, "build", "render-test")

# Make the tripwire module importable, so the custom-widget test is a real
# proof: if the renderer used uic.loadUi it would find and exec evil_widget.
sys.path.insert(0, SAMPLES)

failures: list[str] = []


def check(name: str, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"  FAIL  {name}: {exc}")
        traceback.print_exc(limit=3)


def save(name: str, data: bytes) -> str:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def png_size(data: bytes) -> tuple[int, int]:
    # PNG IHDR: bytes 16..24 are width/height, big endian.
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    import struct
    return struct.unpack(">II", data[16:24])


def test_login_small():
    r = ui_render.render(os.path.join(SAMPLES, "login.ui"), 320, 240)
    assert png_size(r.png) == (320, 240), png_size(r.png)
    assert r.meta["rootClass"] == "QWidget", r.meta
    assert r.meta["childWidgets"] >= 8, r.meta
    assert r.meta["fitScale"] < 1.0, "should have shrunk to fit"
    save("login_320x240.png", r.png)


def test_login_large():
    r = ui_render.render(os.path.join(SAMPLES, "login.ui"), 900, 700)
    assert png_size(r.png) == (900, 700)
    assert r.meta["fitScale"] == 1.0, "must not upscale"
    save("login_900x700.png", r.png)


def test_tabs():
    r = ui_render.render(os.path.join(SAMPLES, "editor.ui"), 640, 480)
    assert png_size(r.png) == (640, 480)
    assert "QTabWidget" in r.meta["classes"], r.meta["classes"]
    save("editor_640x480.png", r.png)


def test_custom_widget_is_placeholder_not_import():
    """A .ui naming an arbitrary <header> must not import or execute it."""
    marker = os.path.join(OUT, "PWNED.txt")
    if os.path.exists(marker):
        os.remove(marker)
    r = ui_render.render(os.path.join(SAMPLES, "customwidget.ui"), 400, 300)
    assert png_size(r.png) == (400, 300)
    assert not os.path.exists(marker), "QUiLoader executed attacker-controlled code!"
    save("customwidget_400x300.png", r.png)


def test_malformed():
    try:
        ui_render.render(os.path.join(SAMPLES, "broken.ui"), 400, 300)
    except ui_render.RenderError:
        return
    raise AssertionError("malformed .ui should raise RenderError")


def test_missing():
    try:
        ui_render.render(os.path.join(SAMPLES, "does-not-exist.ui"), 400, 300)
    except ui_render.RenderError:
        return
    raise AssertionError("missing file should raise RenderError")


def test_error_tile():
    data = ui_render.render_error_tile(300, 120, "Impossible de charger ce fichier .ui")
    assert png_size(data) == (300, 120)
    save("error_tile.png", data)


def test_repeated_renders_no_leak():
    """Many sequential renders in one process must stay stable."""
    for i in range(12):
        r = ui_render.render(os.path.join(SAMPLES, "login.ui"), 300 + i, 220 + i)
        assert png_size(r.png) == (300 + i, 220 + i)


# --- interactive mode -------------------------------------------------------
W, H = 760, 560
LOGIN = os.path.join(SAMPLES, "login.ui")


def _session():
    key = ui_render._session_key(LOGIN, W, H, None, 8)
    return ui_render._sessions[key]


def _canvas(sess, widget, lx, ly):
    """widget-local point -> pane/canvas point (inverse of the fit transform)."""
    root = widget.mapTo(sess.widget, ui_render.QtCore.QPoint(lx, ly))
    cx = sess.off_x + root.x() * sess.drawn_w / sess.widget_w
    cy = sess.off_y + root.y() * sess.drawn_h / sess.widget_h
    return int(round(cx)), int(round(cy))


def _click(sess, widget, lx, ly):
    x, y = _canvas(sess, widget, lx, ly)
    ui_render.render_interactive(LOGIN, W, H, input={"type": "mouse", "action": "press", "x": x, "y": y})
    ui_render.render_interactive(LOGIN, W, H, input={"type": "mouse", "action": "release", "x": x, "y": y})


def test_interactive_checkbox_toggles():
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    chk = sess.widget.findChild(ui_render.QtWidgets.QCheckBox, "rememberCheck")
    assert not chk.isChecked()
    _click(sess, chk, 8, chk.rect().height() // 2)   # indicator
    assert chk.isChecked(), "checkbox did not toggle across press/release requests"
    r = ui_render.render_interactive(LOGIN, W, H)
    save("interactive_checked.png", r.png)


def test_interactive_lineedit_typing():
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    edit = sess.widget.findChild(ui_render.QtWidgets.QLineEdit, "userEdit")
    c = edit.rect().center()
    _click(sess, edit, c.x(), c.y())
    assert edit.hasFocus(), "lineedit did not gain focus from click"
    r = ui_render.render_interactive(LOGIN, W, H, input=[
        {"type": "char", "text": ch} for ch in "selmen"])
    assert edit.text() == "selmen", edit.text()
    assert r.meta["focused"] == "userEdit", r.meta["focused"]
    save("interactive_typed.png", r.png)


def test_interactive_tab_focus_traversal():
    """Tab / Shift+Tab move focus between widgets. sendEvent bypasses Qt's
    notify-level traversal, so the renderer must drive focusNextPrevChild."""
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    app = ui_render.QtWidgets.QApplication.instance()
    user = sess.widget.findChild(ui_render.QtWidgets.QLineEdit, "userEdit")
    pw = sess.widget.findChild(ui_render.QtWidgets.QLineEdit, "passEdit")
    c = user.rect().center()
    _click(sess, user, c.x(), c.y())
    assert user.hasFocus(), "userEdit not focused from click"
    r = ui_render.render_interactive(LOGIN, W, H, input={"type": "key", "key": 0x09})
    assert app.focusWidget() is pw, "Tab did not advance focus to passEdit"
    assert r.meta["focused"] == "passEdit", r.meta["focused"]
    # Typing now lands in the newly focused field, not the previous one.
    ui_render.render_interactive(LOGIN, W, H, input={"type": "char", "text": "secret"})
    assert pw.text() == "secret", pw.text()
    assert user.text() == "", user.text()
    # Shift+Tab walks back to the previous widget.
    rb = ui_render.render_interactive(
        LOGIN, W, H, input={"type": "key", "key": 0x09, "shift": True})
    assert app.focusWidget() is user, "Shift+Tab did not return focus to userEdit"
    assert rb.meta["focused"] == "userEdit", rb.meta["focused"]


def test_interactive_button_clicked():
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    ok = sess.widget.findChild(ui_render.QtWidgets.QPushButton, "okButton")
    hits = []
    ok.clicked.connect(lambda: hits.append(1))
    c = ok.rect().center()
    _click(sess, ok, c.x(), c.y())
    assert len(hits) == 1, f"clicked fired {len(hits)} times"


def test_interactive_reset_clears_state():
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    edit = sess.widget.findChild(ui_render.QtWidgets.QLineEdit, "userEdit")
    c = edit.rect().center()
    _click(sess, edit, c.x(), c.y())
    ui_render.render_interactive(LOGIN, W, H, input={"type": "char", "text": "x"})
    assert edit.text() == "x"
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess2 = _session()
    edit2 = sess2.widget.findChild(ui_render.QtWidgets.QLineEdit, "userEdit")
    assert edit2.text() == "", "reset must drop the live session"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_interactive_hover_highlight_and_cursor():
    """Hover lights up a control (Enter/Leave) and reports a cursor affordance."""
    # Checkbox-only form: no default-button pulse to confound the pixel compare.
    p = os.path.join(SAMPLES, "interactive_check.ui")
    ui_render.render_interactive(p, W, H, reset=True)
    sess = ui_render._sessions[ui_render._session_key(p, W, H, None, 8)]
    chk = sess.widget.findChild(ui_render.QtWidgets.QCheckBox, "flagCheck")
    c = chk.rect().center()
    x, y = _canvas(sess, chk, c.x(), c.y())
    base = _hash(ui_render.render_interactive(p, W, H).png)
    hov = ui_render.render_interactive(
        p, W, H, input={"type": "mouse", "action": "move", "x": x, "y": y})
    assert chk.underMouse(), "hover did not set underMouse (no Enter synthesized)"
    assert hov.meta["cursor"] == "hand", hov.meta["cursor"]
    # The native style fades the hover glow in over a couple of frames; in use
    # the move stream + periodic repaints supply those, so check hover + 2 idle.
    frames = [_hash(hov.png)]
    for _ in range(2):
        frames.append(_hash(ui_render.render_interactive(p, W, H).png))
    assert any(f != base for f in frames), "hover highlight never painted"
    leave = ui_render.render_interactive(p, W, H, input={"type": "mouseleave"})
    assert not chk.underMouse(), "mouseleave did not clear hover"
    assert leave.meta["cursor"] == "arrow", leave.meta["cursor"]
    # I-beam affordance over a line edit.
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    s2 = _session()
    edit = s2.widget.findChild(ui_render.QtWidgets.QLineEdit, "userEdit")
    ec = edit.rect().center()
    ex, ey = _canvas(s2, edit, ec.x(), ec.y())
    er = ui_render.render_interactive(
        LOGIN, W, H, input={"type": "mouse", "action": "move", "x": ex, "y": ey})
    assert er.meta["cursor"] == "ibeam", er.meta["cursor"]
    save("interactive_hover.png", hov.png)


def test_interactive_pressed_state_visible():
    """The press frame must look pressed and the release frame must differ."""
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    ok = sess.widget.findChild(ui_render.QtWidgets.QPushButton, "okButton")
    c = ok.rect().center()
    x, y = _canvas(sess, ok, c.x(), c.y())

    def settle():
        # The product paints the input frame immediately and lets the handler
        # request follow-up frames while the style fade runs; emulate that here
        # by giving the animation timer time before grabbing the settled frame.
        loop = ui_render.QtCore.QEventLoop()
        ui_render.QtCore.QTimer.singleShot(160, loop.quit)
        loop.exec()
        return ui_render.render_interactive(LOGIN, W, H)

    base = _hash(ui_render.render_interactive(LOGIN, W, H).png)
    ui_render.render_interactive(
        LOGIN, W, H, input={"type": "mouse", "action": "press", "x": x, "y": y})
    assert ok.isDown(), "press did not set isDown"
    pr = settle()
    assert _hash(pr.png) != base, "pressed state not painted into the frame"
    ui_render.render_interactive(
        LOGIN, W, H, input={"type": "mouse", "action": "release", "x": x, "y": y})
    assert not ok.isDown(), "release did not clear isDown"
    rl = settle()
    assert _hash(rl.png) != _hash(pr.png), "release frame identical to pressed frame"
    save("interactive_pressed.png", pr.png)


def test_interactive_caret_blinks():
    """Idle repaints while focused must advance the caret blink timer."""
    edit_path = os.path.join(SAMPLES, "interactive_edit.ui")
    ui_render.render_interactive(edit_path, W, H, reset=True)
    key = ui_render._session_key(edit_path, W, H, None, 8)
    sess = ui_render._sessions[key]
    edit = sess.widget.findChild(ui_render.QtWidgets.QLineEdit, "inputEdit")
    c = edit.rect().center()
    x, y = _canvas(sess, edit, c.x(), c.y())
    ui_render.render_interactive(edit_path, W, H,
                                 input={"type": "mouse", "action": "press", "x": x, "y": y})
    ui_render.render_interactive(edit_path, W, H,
                                 input={"type": "mouse", "action": "release", "x": x, "y": y})
    ui_render.render_interactive(edit_path, W, H, input={"type": "char", "text": "ab"})
    assert edit.hasFocus(), "edit not focused for caret test"
    frames = set()
    for _ in range(6):
        frames.add(_hash(ui_render.render_interactive(edit_path, W, H).png))
        time.sleep(0.35)
    assert len(frames) > 1, "caret never blinked across idle repaints"


def test_interactive_dropdown_visible_and_selectable():
    """A combo dropdown must appear in the frame and its items be selectable."""
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    combo = sess.widget.findChild(ui_render.QtWidgets.QComboBox, "profileCombo")
    g = combo.geometry()
    base = ui_render.render_interactive(LOGIN, W, H)
    assert base.meta["popups"] == 0, base.meta
    # Open via a click on the combo; the list is a separate top-level window the
    # form grab excludes, so it must be composited explicitly to be visible.
    _click(sess, combo, g.width() // 2, g.height() // 2)
    opened = ui_render.render_interactive(LOGIN, W, H)
    assert opened.meta["popups"] == 1, opened.meta
    assert _hash(opened.png) != _hash(base.png), "dropdown not painted into frame"
    pops = ui_render._session_popups(sess)
    assert len(pops) == 1, pops
    assert pops[0].testAttribute(
        ui_render.QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen), \
        "popup would leak onto the real desktop"
    save("dropdown_open.png", opened.png)
    # Click the second item -> selects it and closes the popup.
    view = combo.view()
    ir = view.visualRect(combo.model().index(1, 0))
    ig = view.mapToGlobal(ui_render.QtCore.QPoint(
        ir.x() + ir.width() // 2, ir.y() + ir.height() // 2))
    fl = sess.widget.mapFromGlobal(ig)
    ix, iy = _canvas(sess, sess.widget, fl.x(), fl.y())
    ui_render.render_interactive(LOGIN, W, H,
                                 input={"type": "mouse", "action": "press", "x": ix, "y": iy})
    sel = ui_render.render_interactive(
        LOGIN, W, H, input={"type": "mouse", "action": "release", "x": ix, "y": iy})
    assert combo.currentText() == "Enseignant", combo.currentText()
    assert sel.meta["popups"] == 0, sel.meta
    save("dropdown_selected.png", sel.png)
    # Keyboard: reopen, Down+Return selects the next row; Escape closes unchanged.
    _click(sess, combo, g.width() // 2, g.height() // 2)
    assert ui_render.render_interactive(LOGIN, W, H).meta["popups"] == 1
    kb = ui_render.render_interactive(LOGIN, W, H, input=[
        {"type": "key", "key": 0x28}, {"type": "key", "key": 0x0D}])
    assert combo.currentText() == "Administrateur", combo.currentText()
    assert kb.meta["popups"] == 0, kb.meta
    _click(sess, combo, g.width() // 2, g.height() // 2)
    esc = ui_render.render_interactive(LOGIN, W, H, input={"type": "key", "key": 0x1B})
    assert esc.meta["popups"] == 0, esc.meta
    assert combo.currentText() == "Administrateur", "Escape must not change selection"


def test_interactive_popup_outside_click_closes_and_frees_input():
    """An outside press must close the dropdown (native semantics), be consumed,
    and leave later clicks working on other widgets."""
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    combo = sess.widget.findChild(ui_render.QtWidgets.QComboBox, "profileCombo")
    chk = sess.widget.findChild(ui_render.QtWidgets.QCheckBox, "rememberCheck")
    g = combo.geometry()
    _click(sess, combo, g.width() // 2, g.height() // 2)
    assert ui_render.render_interactive(LOGIN, W, H).meta["popups"] == 1
    # Outside press on the checkbox: closes the popup, consumes the click.
    _click(sess, chk, 8, chk.rect().height() // 2)
    r = ui_render.render_interactive(LOGIN, W, H)
    assert r.meta["popups"] == 0, "outside click did not close the dropdown"
    assert not chk.isChecked(), "the closing click must be consumed, not toggle"
    # The next click reaches the checkbox normally.
    _click(sess, chk, 8, chk.rect().height() // 2)
    assert chk.isChecked(), "widget dead after popup closed"
    # Same for a line edit: close, then focus + type on the following click.
    ui_render.render_interactive(LOGIN, W, H, reset=True)
    sess = _session()
    combo = sess.widget.findChild(ui_render.QtWidgets.QComboBox, "profileCombo")
    edit = sess.widget.findChild(ui_render.QtWidgets.QLineEdit, "userEdit")
    g = combo.geometry()
    _click(sess, combo, g.width() // 2, g.height() // 2)
    assert ui_render.render_interactive(LOGIN, W, H).meta["popups"] == 1
    c = edit.rect().center()
    _click(sess, edit, c.x(), c.y())
    assert ui_render.render_interactive(LOGIN, W, H).meta["popups"] == 0
    _click(sess, edit, c.x(), c.y())
    assert edit.hasFocus(), "edit dead after popup closed"
    ui_render.render_interactive(LOGIN, W, H, input={"type": "char", "text": "ok"})
    assert edit.text() == "ok", edit.text()


def test_interactive_real_size_and_scroll():
    """Large forms render at 100% and scroll instead of shrinking to fit."""
    p = os.path.join(SAMPLES, "large_form.ui")
    r0 = ui_render.render_interactive(p, W, H, reset=True)
    assert r0.meta["contentSize"] == [900, 700], r0.meta["contentSize"]
    assert r0.meta["renderedSize"] == [W, H], r0.meta["renderedSize"]
    assert r0.meta["scroll"] == [0, 0], r0.meta["scroll"]
    assert png_size(r0.png) == (W, H)
    save("scroll_top.png", r0.png)
    sess = ui_render._sessions[ui_render._session_key(p, W, H, None, 8)]
    top = sess.widget.findChild(ui_render.QtWidgets.QLabel, "topLabel")
    bot = sess.widget.findChild(ui_render.QtWidgets.QLabel, "bottomLabel")
    assert top.geometry().y() < H, top.geometry()
    assert bot.geometry().y() > H, "bottom label should be below the fold at scroll 0"
    # Scroll down: the viewport shifts and the frame changes.
    r1 = ui_render.render_interactive(p, W, H, scroll=(0, 100))
    assert r1.meta["scroll"] == [0, 100], r1.meta["scroll"]
    assert _hash(r1.png) != _hash(r0.png), "scrolling did not change the viewport"
    save("scroll_mid.png", r1.png)
    # Offsets clamp to the scrollable range (content - box).
    r2 = ui_render.render_interactive(p, W, H, scroll=(99999, 99999))
    assert r2.meta["scroll"] == [900 - W, 700 - H], r2.meta["scroll"]
    save("scroll_bottom.png", r2.png)
    # A form that fits stays centred with zero scroll.
    rl = ui_render.render_interactive(LOGIN, W, H, reset=True)
    assert rl.meta["contentSize"][0] == 380, rl.meta["contentSize"]
    assert rl.meta["contentSize"][1] < H, rl.meta["contentSize"]
    assert rl.meta["scroll"] == [0, 0], rl.meta["scroll"]


def main() -> int:
    print(f"Qt {ui_render.QtCore.qVersion()} / PySide6 {PySide6.__version__}")
    print(f"output -> {OUT}")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            check(name, fn)
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all render tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())