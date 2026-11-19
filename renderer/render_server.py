"""Named-pipe render server: loads .ui forms with QUiLoader, returns PNG bytes.

The C++ preview handler creates the pipe and spawns this process; we connect as
a client and serve framed requests until the pipe closes or we go idle.

Wire format (all lengths big-endian u32):
  request : u32 len + UTF-8 JSON {id, path, width, height, background, margin}
  response: u32 len + UTF-8 JSON {id, ok, png_len, raw_len, err_len, meta_len}
            err_len  bytes raw UTF-8 error text
            meta_len bytes raw UTF-8 metadata JSON
            png_len  bytes raw PNG (empty on the live wire; kept for tooling)
            raw_len  bytes top-down BGRA pixels (width*height*4 from meta)

Runs headless: no window is ever shown; forms are rasterised with QWidget.grab.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ui_render  # noqa: E402

import win32file  # noqa: E402
import win32pipe  # noqa: E402
import pywintypes  # noqa: E402

_last_activity = time.monotonic()


def _touch() -> None:
    global _last_activity
    _last_activity = time.monotonic()


def _read_exact(handle, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        hr, data = win32file.ReadFile(handle, min(remaining, 1 << 20))
        if hr != 0 or not data:
            raise ConnectionError(f"ReadFile hr={hr} got={len(data)} want={remaining}")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _write_exact(handle, data: bytes) -> None:
    view = memoryview(data)
    while view:
        hr, written = win32file.WriteFile(handle, view[: 1 << 20])
        if hr != 0 or written <= 0:
            raise ConnectionError(f"WriteFile hr={hr} written={written}")
        view = view[written:]


def _read_frame(handle) -> dict:
    (length,) = struct.unpack(">I", _read_exact(handle, 4))
    if length > 64 * 1024 * 1024:
        raise ConnectionError(f"absurd request length {length}")
    return json.loads(_read_exact(handle, length).decode("utf-8"))


def _send(handle, req_id: int, ok: bool, png: bytes, err: str, meta: dict,
          raw: bytes = b"") -> None:
    err_b = err.encode("utf-8")
    meta_b = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    header = json.dumps({
        "id": req_id, "ok": ok,
        "png_len": len(png), "raw_len": len(raw),
        "err_len": len(err_b), "meta_len": len(meta_b),
    }).encode("utf-8")
    out = struct.pack(">I", len(header)) + header + err_b + meta_b + png + raw
    _write_exact(handle, out)


def _handle_request(handle, req: dict) -> None:
    req_id = int(req.get("id", 0))
    path = req.get("path", "")
    width = int(req.get("width", 320))
    height = int(req.get("height", 240))
    background = req.get("background") or None
    margin = int(req.get("margin", 8))
    try:
        result = ui_render.render_interactive(
            path, width, height, background=background, margin=margin,
            input=req.get("input"), reset=bool(req.get("reset")),
            scroll=(int(req.get("scrollX", 0)), int(req.get("scrollY", 0))),
            encode_png=False)
        _send(handle, req_id, True, result.png, "", result.meta, result.raw)
    except ui_render.RenderError as exc:
        _send(handle, req_id, False, b"", str(exc), {})
    except Exception as exc:  # noqa: BLE001 - a bad file must not kill the server
        traceback.print_exc()
        _send(handle, req_id, False, b"", f"{type(exc).__name__}: {exc}", {})


def _idle_watchdog(timeout: int) -> None:
    while True:
        time.sleep(2)
        if time.monotonic() - _last_activity > timeout:
            os._exit(0)


def serve(pipe_name: str, idle: int) -> int:
    ui_render.ensure_app()
    handle = None
    for attempt in range(60):
        try:
            handle = win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            break
        except pywintypes.error:
            time.sleep(0.1)
    if handle is None:
        print(f"could not connect to {pipe_name}", file=sys.stderr)
        return 2

    threading.Thread(target=_idle_watchdog, args=(idle,), daemon=True).start()
    _touch()
    try:
        while True:
            try:
                req = _read_frame(handle)
            except (ConnectionError, OSError, ValueError):
                break
            _touch()
            _handle_request(handle, req)
            _touch()
    finally:
        try:
            win32file.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Qt .ui preview render server")
    parser.add_argument("--pipe", required=True)
    parser.add_argument("--idle", type=int, default=90)
    args = parser.parse_args()
    return serve(args.pipe, args.idle)


if __name__ == "__main__":
    raise SystemExit(main())