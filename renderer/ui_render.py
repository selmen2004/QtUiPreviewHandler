"""Core .ui rendering: load a Qt Designer form with QUiLoader and rasterise it.

Deliberately uses QUiLoader (XML-driven widget construction) rather than
PyQt6's uic.loadUi, which generates and execs Python source. A preview handler
runs when a file is merely *selected* in Explorer, so executing code derived
from the file is not acceptable: uic emits `from <header> import <class>` for
every <customwidget>, which is an arbitrary-import vector. QUiLoader instead
creates a plain placeholder widget for unknown classes and never imports.

This module has no IPC and no window of its own; it is importable and testable
on its own. render() returns PNG bytes for a box of exactly (width, height).
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

# Qt must not try to use a real screen session when we only want pixels.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

from PySide6 import QtCore, QtGui, QtWidgets, QtUiTools  # noqa: E402


class RenderError(Exception):
    """The .ui could not be loaded or rasterised."""


@dataclass
class RenderResult:
    png: bytes
    width: int
    height: int
    meta: dict = field(default_factory=dict)
    raw: bytes = b""   # top-down BGRA pixels; the pipe wire uses this, not PNG


_app: QtWidgets.QApplication | None = None
_guard: QtCore.QObject | None = None


class _OffscreenGuard(QtCore.QObject):
    """Force every top-level widget offscreen.

    A form's combo-box popup (QComboBoxPrivateContainer) and menus are separate
    top-level windows that do NOT inherit the form's WA_DontShowOnScreen, so
    without this they would map onto the user's real desktop. Setting the
    attribute on Show keeps them rasterisable via grab() but never visible.
    """

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt override
        if ev.type() == QtCore.QEvent.Type.Show and isinstance(obj, QtWidgets.QWidget):
            if obj.isWindow():
                obj.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        return False


def ensure_app() -> QtWidgets.QApplication:
    """One QApplication per process, created on the calling thread."""
    global _app, _guard
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1])
        app.setApplicationName("QtUiPreviewRenderer")
        app.setOrganizationName("QtUiPreviewHandler")
        # Never let a form's own code pop a modal dialog into the shell.
        app.setQuitOnLastWindowClosed(False)
        _guard = _OffscreenGuard()
        app.installEventFilter(_guard)
    _app = app
    return app


def _declared_geometry(widget: QtWidgets.QWidget) -> QtCore.QSize | None:
    """The size the designer wrote into the .ui, if any."""
    size = widget.size()
    if size.width() > 0 and size.height() > 0:
        return size
    return None


def load_form(path: str) -> QtWidgets.QWidget:
    info = QtCore.QFileInfo(path)
    if not info.exists():
        raise RenderError(f"File not found: {path}")
    if not info.isFile():
        raise RenderError(f"Not a file: {path}")

    loader = QtUiTools.QUiLoader()
    # Relative image paths in <pixmap> resolve against the .ui's own folder.
    loader.setWorkingDirectory(info.absoluteDir())

    handle = QtCore.QFile(path)
    if not handle.open(QtCore.QIODeviceBase.OpenModeFlag.ReadOnly):
        raise RenderError(f"Cannot open for reading: {path}")
    try:
        widget = loader.load(handle, None)
    except RuntimeError as exc:
        # QUiLoader raises RuntimeError("Unable to open/read ui device") on
        # malformed XML; surface it as a normal preview failure.
        raise RenderError(f"Invalid Qt Designer file: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - never leak into the shell host
        raise RenderError(f"Could not load form: {exc}") from exc
    finally:
        handle.close()

    if widget is None:
        raise RenderError("QUiLoader returned no widget - not a valid Qt Designer .ui file")
    return widget


def _collect_meta(widget: QtWidgets.QWidget, path: str) -> dict:
    children = widget.findChildren(QtWidgets.QWidget)
    classes: dict[str, int] = {}
    for child in children:
        name = child.metaObject().className()
        classes[name] = classes.get(name, 0) + 1
    return {
        "file": os.path.basename(path),
        "rootClass": widget.metaObject().className(),
        "objectName": widget.objectName(),
        "windowTitle": widget.windowTitle(),
        "childWidgets": len(children),
        "classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])[:12]),
        "hasLayout": widget.layout() is not None,
    }


def _fit(natural: QtCore.QSize, minimum: QtCore.QSize,
         box_w: int, box_h: int, margin: int) -> tuple[int, int, float]:
    """Shrink-to-fit, never upscale. Returns (w, h, scale_applied)."""
    avail_w = max(1, box_w - 2 * margin)
    avail_h = max(1, box_h - 2 * margin)
    nw = max(1, natural.width())
    nh = max(1, natural.height())
    scale = min(avail_w / nw, avail_h / nh, 1.0)
    w = max(1, int(round(nw * scale)))
    h = max(1, int(round(nh * scale)))
    # A form with fixed-size children cannot shrink past its minimum; the
    # pixmap is scaled down afterwards instead.
    w = max(w, minimum.width())
    h = max(h, minimum.height())
    return w, h, scale


def render(path: str, width: int, height: int, *,
           background: str | None = None,
           margin: int = 8,
           dpr: float = 1.0) -> RenderResult:
    """Rasterise `path` into a PNG of exactly width x height physical pixels."""
    if width < 16 or height < 16:
        raise RenderError(f"Preview box too small: {width}x{height}")

    app = ensure_app()
    widget = load_form(path)
    try:
        widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.ensurePolished()

        natural = _declared_geometry(widget) or widget.sizeHint()
        if natural.width() <= 0 or natural.height() <= 0:
            natural = widget.sizeHint()
        if natural.width() <= 0 or natural.height() <= 0:
            raise RenderError("Form has no determinable size")

        minimum = widget.minimumSizeHint()
        target_w, target_h, scale = _fit(natural, minimum, width, height, margin)

        widget.resize(target_w, target_h)
        if widget.layout() is not None:
            widget.layout().activate()
        # Layout/sizeHint propagation is posted, not synchronous.
        for _ in range(3):
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)

        pixmap = widget.grab()
        if pixmap.isNull():
            raise RenderError("QWidget.grab() produced a null pixmap")

        canvas = QtGui.QPixmap(width, height)
        canvas.fill(QtGui.QColor(background) if background else QtGui.QColor(255, 255, 255))

        # If the form refused to shrink to the box, scale the pixels smoothly.
        if pixmap.width() > width - 2 * margin or pixmap.height() > height - 2 * margin:
            pixmap = pixmap.scaled(
                max(1, width - 2 * margin), max(1, height - 2 * margin),
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )

        painter = QtGui.QPainter(canvas)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        x = (width - pixmap.width()) // 2
        y = (height - pixmap.height()) // 2
        painter.drawPixmap(x, y, pixmap)
        painter.end()

        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODeviceBase.OpenModeFlag.ReadWrite)
        if not canvas.save(buffer, "PNG"):
            raise RenderError("Failed to encode PNG")
        png = bytes(buffer.data())
        buffer.close()

        meta = _collect_meta(widget, path)
        meta.update({
            "naturalSize": [natural.width(), natural.height()],
            "renderedSize": [pixmap.width(), pixmap.height()],
            "fitScale": round(scale, 4),
            "qtVersion": QtCore.qVersion(),
        })
        return RenderResult(png=png, width=width, height=height, meta=meta)
    finally:
        widget.deleteLater()
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)


# ---------------------------------------------------------------------------
# Interactive mode: a LIVE widget session per (path, box). The pane forwards
# mouse/keyboard events here; we apply them to the persistent QWidget and
# re-rasterise. Only built-in widget behaviour runs (QUiLoader wires no user
# code), so clicking/typing stays as safe as the static preview.
# ---------------------------------------------------------------------------

# Windows virtual-key -> Qt.Key for the editing/navigation keys we forward.
_VK_TO_QT = {
    0x08: QtCore.Qt.Key.Key_Backspace,
    0x09: QtCore.Qt.Key.Key_Tab,
    0x0D: QtCore.Qt.Key.Key_Return,
    0x1B: QtCore.Qt.Key.Key_Escape,
    0x20: QtCore.Qt.Key.Key_Space,
    0x21: QtCore.Qt.Key.Key_PageUp,
    0x22: QtCore.Qt.Key.Key_PageDown,
    0x23: QtCore.Qt.Key.Key_End,
    0x24: QtCore.Qt.Key.Key_Home,
    0x25: QtCore.Qt.Key.Key_Left,
    0x26: QtCore.Qt.Key.Key_Up,
    0x27: QtCore.Qt.Key.Key_Right,
    0x28: QtCore.Qt.Key.Key_Down,
    0x2D: QtCore.Qt.Key.Key_Insert,
    0x2E: QtCore.Qt.Key.Key_Delete,
}


@dataclass
class Session:
    widget: QtWidgets.QWidget
    widget_w: int
    widget_h: int
    margin: int = 8
    off_x: int = 0
    off_y: int = 0
    drawn_w: int = 1
    drawn_h: int = 1
    mouse_down: bool = False
    last_focus: object = None   # child to restore when the pane regains focus
    hover_widget: object = None  # widget currently under the pointer (Enter/Leave)
    cursor: str = "arrow"        # cursor affordance for the view's WM_SETCURSOR
    anim_pending: bool = False   # a tactile state change needs the style animation
    anim_until: float = 0.0      # wall-clock deadline while a style fade is running


_sessions: dict[tuple, Session] = {}
_MAX_SESSIONS = 4


def _session_key(path, width, height, background, margin):
    return (os.path.abspath(path), int(width), int(height), background or "", int(margin))


def _modifiers(ev: dict) -> QtCore.Qt.KeyboardModifiers:
    m = QtCore.Qt.KeyboardModifier.NoModifier
    if ev.get("shift"):
        m |= QtCore.Qt.KeyboardModifier.ShiftModifier
    if ev.get("ctrl"):
        m |= QtCore.Qt.KeyboardModifier.ControlModifier
    if ev.get("alt"):
        m |= QtCore.Qt.KeyboardModifier.AltModifier
    return m


def _get_session(path, width, height, background, margin) -> Session:
    key = _session_key(path, width, height, background, margin)
    sess = _sessions.get(key)
    if sess is not None:
        return sess
    app = ensure_app()
    widget = load_form(path)
    widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.ensurePolished()
    natural = _declared_geometry(widget) or widget.sizeHint()
    if natural.width() <= 0 or natural.height() <= 0:
        natural = widget.sizeHint()
    if natural.width() <= 0 or natural.height() <= 0:
        widget.deleteLater()
        raise RenderError("Form has no determinable size")
    minimum = widget.minimumSizeHint()
    # Real-size (100%) interactive preview: never shrink to fit. When the form
    # exceeds the pane, the handler scrolls a viewport onto it instead, so text
    # and controls stay at their designed size and remain readable.
    tw = max(natural.width(), minimum.width())
    th = max(natural.height(), minimum.height())
    widget.resize(tw, th)
    if widget.layout() is not None:
        widget.layout().activate()
    # Visible-to-Qt but never on screen: gives real focus + mouse grabbing so
    # press/release survive across separate pipe requests.
    widget.show()
    app.setActiveWindow(widget)
    for _ in range(3):
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
    sess = Session(widget=widget, widget_w=tw, widget_h=th, margin=margin)
    _sessions[key] = sess
    while len(_sessions) > _MAX_SESSIONS:
        _old_key, old = next(iter(_sessions.items()))
        if old is sess:
            break
        _sessions.pop(_old_key, None)
        old.widget.deleteLater()
    app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)
    return sess


def _canvas_to_widget(sess: Session, x: float, y: float) -> tuple[float, float]:
    wx = (x - sess.off_x) * sess.widget_w / max(1, sess.drawn_w)
    wy = (y - sess.off_y) * sess.widget_h / max(1, sess.drawn_h)
    return wx, wy


def _session_popups(sess: Session) -> list:
    """Visible popup top-levels owned by this form (combo dropdowns, menus).

    These are separate top-level windows, so QWidget.grab() on the form never
    includes them and childAt() on the form never targets their items. We find
    them by walking the parent chain back to the session widget and matching the
    Popup/ToolTip window flag, then composite and route input to them explicitly.
    """
    app = QtWidgets.QApplication.instance()
    out = []
    for w in app.topLevelWidgets():
        if w is sess.widget or not w.isVisible():
            continue
        p = w.parentWidget()
        owned = False
        while p is not None:
            if p is sess.widget:
                owned = True
                break
            p = p.parentWidget()
        if not owned:
            continue
        if w.windowFlags() & (QtCore.Qt.WindowType.Popup | QtCore.Qt.WindowType.ToolTip):
            out.append(w)
    return out


def _close_popups(sess: Session) -> None:
    """Hide every open owned popup through its combo so internal state resets.

    Real Qt closes an open popup on an outside mouse press / focus loss; the
    offscreen session must do the same or the dropdown stays up and swallows
    all later input.
    """
    for pop in _session_popups(sess):
        w = pop.parentWidget()
        combo = None
        while w is not None:
            if isinstance(w, QtWidgets.QComboBox):
                combo = w
                break
            w = w.parentWidget()
        if combo is not None:
            combo.hidePopup()
            # Real Qt hands focus back to the combo when its popup closes;
            # offscreen the hidden list view would otherwise keep focus and
            # block every later widget from receiving it.
            combo.setFocus(QtCore.Qt.FocusReason.PopupFocusReason)
        else:
            pop.hide()
    # Showing a popup makes its container the active top-level; hiding it does
    # not hand activation back offscreen. Re-activate the form (as a real window
    # manager would) so focus/hasFocus resolve to the form's widgets again.
    QtWidgets.QApplication.instance().setActiveWindow(sess.widget)


def _resolve_target(sess: Session, root_pt: QtCore.QPoint):
    """Pick the widget that should receive an event at a form-widget point.

    When a popup is open and the point lands inside its global rect, route into
    the popup (so dropdown items can be hovered/clicked); otherwise target the
    form's own child. Returns (target, local_pos, global_pos).
    """
    glob = sess.widget.mapToGlobal(root_pt)
    for pop in _session_popups(sess):
        if pop.geometry().contains(glob):
            target = pop.childAt(pop.mapFromGlobal(glob)) or pop
            return target, target.mapFromGlobal(glob), glob
    target = sess.widget.childAt(root_pt) or sess.widget
    local = target.mapFrom(sess.widget, root_pt)
    return target, local, glob


def _commit_popup_click(sess: Session, target: QtWidgets.QWidget,
                        glob: QtCore.QPoint, kind: QtCore.QEvent.Type) -> None:
    """Select a combo dropdown item clicked with the mouse.

    Offscreen, QComboBox commits via the view's keyboard `activated` signal, not
    the mouse `clicked` signal (verified: emitting clicked is a no-op here), so a
    plain synthesized click highlights a row but never selects it. Drive the same
    activation path the keyboard uses: set the clicked row, then send Return,
    which fires activated -> setCurrentIndex + hidePopup. Clicking the popup frame
    (no valid item) just closes it.
    """
    if kind != QtCore.QEvent.Type.MouseButtonRelease:
        return
    view = None
    w = target
    while w is not None:
        if isinstance(w, QtWidgets.QAbstractItemView):
            view = w
        if isinstance(w, QtWidgets.QComboBox):
            break
        w = w.parentWidget()
    if view is None or not isinstance(w, QtWidgets.QComboBox):
        return
    combo = w
    idx = view.indexAt(view.viewport().mapFromGlobal(glob))
    if idx.isValid():
        view.setCurrentIndex(idx)
        QtWidgets.QApplication.sendEvent(
            view, QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                  QtCore.Qt.Key.Key_Return,
                                  QtCore.Qt.KeyboardModifier.NoModifier))
    else:
        combo.hidePopup()


def _cursor_for(target: QtWidgets.QWidget) -> str:
    """Map the hovered widget to a cursor affordance for the view window.

    A bare synthesized MouseMove does not set QWidget.underMouse(), so hover
    highlights and the I-beam over edits would never appear; we drive them from
    explicit Enter/Leave plus the widget's own cursor shape. Clickable controls
    get a hand pointer as an affordance even though Qt defaults them to arrow.
    """
    shape = target.cursor().shape()
    if shape == QtCore.Qt.CursorShape.IBeamCursor:
        return "ibeam"
    if shape == QtCore.Qt.CursorShape.PointingHandCursor:
        return "hand"
    if isinstance(target, QtWidgets.QAbstractButton):
        return "hand"
    return "arrow"


def _update_hover(sess: Session, target: QtWidgets.QWidget) -> None:
    """Synthesize Enter/Leave when the hovered widget changes.

    Qt only sets underMouse()/hover state from real Enter/Leave events, which an
    offscreen session never receives on its own. Sending them makes buttons and
    edits light up on hover exactly as they would on screen.
    """
    if target is sess.hover_widget:
        sess.cursor = _cursor_for(target)
        return
    app = QtWidgets.QApplication.instance()
    old = sess.hover_widget
    if old is not None:
        try:
            QtWidgets.QApplication.sendEvent(old, QtCore.QEvent(QtCore.QEvent.Type.Leave))
        except RuntimeError:
            pass
    try:
        QtWidgets.QApplication.sendEvent(target, QtCore.QEvent(QtCore.QEvent.Type.Enter))
    except RuntimeError:
        pass
    sess.hover_widget = target
    sess.cursor = _cursor_for(target)
    sess.anim_pending = True   # let the hover glow animate in before we grab


def _mouse(sess: Session, ev: dict) -> None:
    kind_map = {
        "press": QtCore.QEvent.Type.MouseButtonPress,
        "release": QtCore.QEvent.Type.MouseButtonRelease,
        "move": QtCore.QEvent.Type.MouseMove,
        "dblclick": QtCore.QEvent.Type.MouseButtonDblClick,
    }
    kind = kind_map.get(ev.get("action", "press"), QtCore.QEvent.Type.MouseButtonPress)
    btn = {"left": QtCore.Qt.MouseButton.LeftButton,
           "right": QtCore.Qt.MouseButton.RightButton,
           "middle": QtCore.Qt.MouseButton.MiddleButton}.get(
               ev.get("button", "left"), QtCore.Qt.MouseButton.LeftButton)
    wx, wy = _canvas_to_widget(sess, ev.get("x", 0), ev.get("y", 0))
    root_pt = QtCore.QPoint(int(round(wx)), int(round(wy)))
    if kind == QtCore.QEvent.Type.MouseButtonPress:
        pops = _session_popups(sess)
        if pops:
            glob0 = sess.widget.mapToGlobal(root_pt)
            if not any(p.geometry().contains(glob0) for p in pops):
                # Native semantics: an outside press closes the open popup and is
                # consumed (it does not also activate the widget underneath). The
                # next click then reaches its widget normally.
                _close_popups(sess)
                sess.mouse_down = False
                sess.anim_pending = True
                return
    target, local, glob = _resolve_target(sess, root_pt)
    _update_hover(sess, target)
    if kind == QtCore.QEvent.Type.MouseButtonPress:
        # Focus-on-click is performed by the platform window layer (QWidgetWindow)
        # in a real window, before the press reaches the widget. Synthesized
        # widget-level events bypass it, so do it here for click-focusable widgets.
        if target.isEnabled() and (target.focusPolicy() & QtCore.Qt.FocusPolicy.ClickFocus):
            target.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        sess.mouse_down = True
        held = btn
    elif kind == QtCore.QEvent.Type.MouseButtonRelease:
        sess.mouse_down = False
        held = QtCore.Qt.MouseButton.NoButton
    else:
        held = btn if sess.mouse_down else QtCore.Qt.MouseButton.NoButton
    qev = QtGui.QMouseEvent(kind, QtCore.QPointF(local), QtCore.QPointF(glob),
                            btn, held, _modifiers(ev))
    # notify() (not sendEvent) runs QApplication's real dispatch: popup close on
    # outside press, focus restoration and implicit mouse grab, so the session
    # behaves like a real window instead of leaking popup/focus state.
    QtWidgets.QApplication.instance().notify(target, qev)
    _commit_popup_click(sess, target, glob, kind)
    if kind in (QtCore.QEvent.Type.MouseButtonPress,
                QtCore.QEvent.Type.MouseButtonRelease):
        # The down/up visual is animated by the style; flag it so the frame is
        # rasterised after the animation advances (see render_interactive).
        sess.anim_pending = True


def _key(sess: Session, ev: dict) -> None:
    app = QtWidgets.QApplication.instance()
    focus = app.focusWidget() or sess.widget
    mods = _modifiers(ev)
    if ev.get("type") == "char":
        qev = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, 0, mods, ev.get("text", ""))
    else:
        qt_key = _VK_TO_QT.get(int(ev.get("key", 0)), QtCore.Qt.Key.Key_unknown)
        kind = (QtCore.QEvent.Type.KeyRelease if ev.get("action") == "release"
                else QtCore.QEvent.Type.KeyPress)
        qev = QtGui.QKeyEvent(kind, qt_key, mods, ev.get("text", ""))
    QtWidgets.QApplication.sendEvent(focus, qev)
    # sendEvent bypasses QApplication::notify, which is where Qt normally turns
    # Tab / Shift+Tab into focus traversal. Do it explicitly when the focused
    # widget left the key unaccepted (a QLineEdit ignores Tab; a QTextEdit
    # consumes it to insert a tab, so we must not override that).
    if (ev.get("type") != "char"
            and kind == QtCore.QEvent.Type.KeyPress
            and not qev.isAccepted()
            and qt_key in (QtCore.Qt.Key.Key_Tab, QtCore.Qt.Key.Key_Backtab)):
        forward = qt_key == QtCore.Qt.Key.Key_Tab
        if bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier):
            forward = not forward
        try:
            focus.focusNextPrevChild(forward)
        except RuntimeError:
            pass


def _wheel(sess: Session, ev: dict) -> None:
    wx, wy = _canvas_to_widget(sess, ev.get("x", 0), ev.get("y", 0))
    root_pt = QtCore.QPoint(int(round(wx)), int(round(wy)))
    target, local, glob = _resolve_target(sess, root_pt)
    delta = int(ev.get("delta", 0))
    qev = QtGui.QWheelEvent(QtCore.QPointF(local), QtCore.QPointF(glob),
                            QtCore.QPoint(0, 0), QtCore.QPoint(0, delta),
                            QtCore.Qt.MouseButton.NoButton, _modifiers(ev),
                            QtCore.Qt.ScrollPhase.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(target, qev)


def apply_input(sess: Session, events) -> None:
    if isinstance(events, dict):
        events = [events]
    for ev in events or []:
        t = ev.get("type")
        if t == "mouse":
            _mouse(sess, ev)
        elif t in ("key", "char"):
            _key(sess, ev)
        elif t == "wheel":
            _wheel(sess, ev)
        elif t == "mouseleave":
            # Pointer left the view window: drop hover highlight + cursor.
            old = sess.hover_widget
            if old is not None:
                try:
                    QtWidgets.QApplication.sendEvent(
                        old, QtCore.QEvent(QtCore.QEvent.Type.Leave))
                except RuntimeError:
                    pass
            sess.hover_widget = None
            sess.cursor = "arrow"
            sess.anim_pending = True
        elif t == "focus":
            app = QtWidgets.QApplication.instance()
            if ev.get("gained"):
                # Restore the previously focused child. Never focus the bare
                # top-level form: holding focus there blocks focus-on-click for
                # children, so a subsequent click would not focus a line edit.
                if sess.last_focus is not None:
                    try:
                        sess.last_focus.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
                    except RuntimeError:
                        sess.last_focus = None
            else:
                _close_popups(sess)
                fw = app.focusWidget()
                if fw is not None:
                    if fw is not sess.widget:
                        sess.last_focus = fw
                    fw.clearFocus()
    # Flushing posted events while a button is held would drop the mouse grab
    # and swallow the matching release; only flush once the click completes.
    if not sess.mouse_down:
        app = QtWidgets.QApplication.instance()
        for _ in range(2):
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)


def _raster_session(sess: Session, width: int, height: int,
                    background: str | None,
                    scroll_x: int = 0, scroll_y: int = 0,
                    want_png: bool = True) -> tuple[bytes, bytes, dict]:
    """Rasterise a box-sized viewport onto the real-size (100%) form.

    The form is never scaled. If it fits the box it is centred; if it overflows,
    the requested scroll offset selects which part is visible (clamped to the
    scrollable range). Input mapping and popup placement both derive from
    off_x/off_y, so they stay correct at any scroll position.
    """
    pixmap = sess.widget.grab()
    if pixmap.isNull():
        raise RenderError("QWidget.grab() produced a null pixmap")
    cw, ch = sess.widget_w, sess.widget_h
    if cw <= width:
        sx = 0
        sess.off_x = (width - cw) // 2
    else:
        sx = max(0, min(int(scroll_x), cw - width))
        sess.off_x = -sx
    if ch <= height:
        sy = 0
        sess.off_y = (height - ch) // 2
    else:
        sy = max(0, min(int(scroll_y), ch - height))
        sess.off_y = -sy
    sess.drawn_w = cw
    sess.drawn_h = ch
    canvas = QtGui.QPixmap(width, height)
    canvas.fill(QtGui.QColor(background) if background else QtGui.QColor(255, 255, 255))
    painter = QtGui.QPainter(canvas)
    painter.drawPixmap(sess.off_x, sess.off_y, pixmap)
    # Overlay open popups (combo dropdown / menu) at the same 1:1 scale.
    popups = 0
    for pop in _session_popups(sess):
        pop.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        pm = pop.grab()
        if pm.isNull():
            continue
        tl = sess.widget.mapFromGlobal(pop.mapToGlobal(QtCore.QPoint(0, 0)))
        painter.drawPixmap(sess.off_x + tl.x(), sess.off_y + tl.y(), pm)
        popups += 1
    painter.end()
    # Raw top-down BGRA for the pipe: PNG encode + GDI+ decode cost ~30-40 ms per
    # frame, which dominated input-to-paint latency. The DIB blit is a memcpy.
    img = canvas.toImage().convertToFormat(QtGui.QImage.Format.Format_ARGB32)
    raw = bytes(img.constBits())
    png = b""
    if want_png:
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODeviceBase.OpenModeFlag.ReadWrite)
        if not canvas.save(buffer, "PNG"):
            raise RenderError("Failed to encode PNG")
        png = bytes(buffer.data())
        buffer.close()
    return raw, png, {"popups": popups, "contentSize": [cw, ch], "scroll": [sx, sy]}


def render_interactive(path: str, width: int, height: int, *,
                       background: str | None = None, margin: int = 8,
                       input=None, reset: bool = False,
                       scroll=(0, 0), encode_png: bool = True) -> RenderResult:
    """Render a LIVE session, optionally applying forwarded input events first.

    `scroll` is the pane's (x, y) offset into the real-size form; the renderer
    clamps it and reports the effective value plus the full content size so the
    handler can drive its scrollbars.
    """
    if width < 16 or height < 16:
        raise RenderError(f"Preview box too small: {width}x{height}")
    ensure_app()
    key = _session_key(path, width, height, background, margin)
    if reset:
        old = _sessions.pop(key, None)
        if old is not None:
            old.widget.deleteLater()
    sess = _get_session(path, width, height, background, margin)
    if input:
        apply_input(sess, input)
    elif not sess.mouse_down:
        # No input this frame (e.g. the view's periodic caret-blink repaint):
        # still spin the event loop briefly so blink/animation timers advance.
        app = QtWidgets.QApplication.instance()
        for _ in range(2):
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)
    if sess.anim_pending:
        # Press/tick/text paint immediately; only the style's hover/press fade
        # needs timer time. Blocking the response on a nested 150 ms event loop
        # made every click cost >=150 ms before the frame was even grabbed, so
        # instead report the pending animation and let the handler request
        # follow-up frames until the fade deadline passes (see ApplyResult).
        # isDown and the mouse grab survive across requests either way.
        sess.anim_pending = False
        sess.anim_until = time.monotonic() + 0.25
    anim = time.monotonic() < sess.anim_until
    raw, png, rmeta = _raster_session(sess, width, height, background,
                                      scroll[0], scroll[1], want_png=encode_png)
    meta = _collect_meta(sess.widget, path)
    fw = QtWidgets.QApplication.instance().focusWidget()
    meta.update({
        "interactive": True,
        "focused": (fw.objectName() or fw.metaObject().className()) if fw else None,
        "cursor": sess.cursor,
        "popups": rmeta.get("popups", 0),
        "anim": anim,
        "contentSize": rmeta.get("contentSize", [sess.widget_w, sess.widget_h]),
        "scroll": rmeta.get("scroll", [0, 0]),
        "renderedSize": [width, height],
        "raw": True,
        "qtVersion": QtCore.qVersion(),
    })
    return RenderResult(png=png, width=width, height=height, meta=meta, raw=raw)


def render_error_tile(width: int, height: int, message: str,
                      background: str | None = None) -> bytes:
    """A readable placeholder when the form cannot be rendered."""
    ensure_app()
    canvas = QtGui.QPixmap(max(16, width), max(16, height))
    canvas.fill(QtGui.QColor(background) if background else QtGui.QColor(255, 255, 255))
    painter = QtGui.QPainter(canvas)
    painter.setPen(QtGui.QColor(150, 40, 40))
    font = painter.font()
    font.setPointSize(10)
    painter.setFont(font)
    rect = canvas.rect().adjusted(12, 12, -12, -12)
    painter.drawText(rect,
                     QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.TextFlag.TextWordWrap,
                     message)
    painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODeviceBase.OpenModeFlag.ReadWrite)
    canvas.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data