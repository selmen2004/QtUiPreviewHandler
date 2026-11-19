# QtUiPreviewHandler

A real Windows Explorer **preview-pane** handler for Qt Designer `.ui` files.
Select a `.ui` in Explorer with the preview pane open and the actual form is
rendered inside the pane — layouts, widgets, fonts and all — using Qt's own
`QUiLoader`. The preview is **interactive**: click buttons, toggle checkboxes,
and type into line edits right in the pane.

Native **x64** COM `IPreviewHandler` DLL (MSVC), registered per-user (HKCU, no
admin). All Qt/Python work happens in a **separate renderer process**, so a
malformed or hostile `.ui` can never crash or hang the shell host.

```
Explorer (preview pane)
  └─ prevhost.exe                      64-bit Preview Handler Surrogate Host
       └─ QtUiPreviewHandler.dll       native C++ COM, no Qt/Python linked
            │  IInitializeWithFile / IPreviewHandler / IPreviewHandlerVisuals
            │  IObjectWithSite / IOleWindow
            └─ named pipe ──> pythonw.exe renderer/render_server.py
                                 └─ PySide6 QUiLoader.load(.ui) -> live QWidget session
                                    input events applied, offscreen raster -> raw BGRA
            └─ WS_CHILD view window blits the frame and forwards pane input back
```

## Why these choices

- **x64 native DLL.** 64-bit `prevhost.exe` cannot load 32-bit handlers. The
  earlier `G:\Program Files\QtPreviewLite\QtPreviewLite.dll` was an x86 .NET 4.8
  assembly with a placeholder CLSID `{12345678-…}` and hand-rolled XML parsing;
  it could never load into the x64 surrogate. This one is x64 with a real CLSID.
- **`QUiLoader`, not `uic.loadUi`.** PyQt6's `loadUi` *generates and execs Python*
  and emits `from <header> import <class>` for every `<customwidget>` — an
  arbitrary-import/code-execution vector. A preview handler fires on mere file
  *selection*, so executing file-derived code is unacceptable. `QUiLoader`
  (what Designer itself uses) builds widgets from XML and makes a harmless
  placeholder for unknown classes. `tests/test_render.py` includes a tripwire
  module proving no import occurs.
- **Out-of-process renderer.** Keeps Qt, Python and the GIL out of `prevhost.exe`.
  The DLL only does pipe IO on worker threads and blits a bitmap on the STA
  thread; a bad file yields an error tile, not a shell crash.
- **`SystemFileAssociations\.ui\shellex\{8895b1c6-…}`** for the association, so
  your existing `.ui` ProgId / double-click behaviour is untouched.

## Interactive preview

The pane is a **live widget**, not a static screenshot. The renderer keeps an
offscreen `QWidget` session per (file, size, background); the view window
forwards mouse/keyboard input over the same pipe, the session applies real
`QMouseEvent`/`QKeyEvent`s, and the re-rasterised frame is blitted back. So you
can click buttons, toggle checkboxes/radios, open combos, and click a line edit
and type into it — all inside the Explorer preview pane.

- The DLL drains an **ordered command queue** on a single pump thread and
  **coalesces** everything queued into one request: inputs are applied in
  arrival order, the newest size/scroll wins, and a reset drops stale inputs.
  Only the newest frame is painted.
- The live wire carries **raw top-down BGRA** (no PNG encode/decode on the input
  path); the C++ side blits it via a negative-height `CreateDIBSection`. PNG is
  kept only for the renderer tests and capture tooling.
- The session is shown to Qt with `WA_DontShowOnScreen` + `setActiveWindow`, giving
  real focus and mouse-grab behaviour without ever putting a window on screen.
  Combo/list **popups get native semantics**: an outside press closes the popup
  and is consumed (it does not also hit the widget underneath), focus returns to
  the combo, and clicking a widget focuses it per its `focusPolicy`.
- Style fades (hover/press/tick transitions) settle via chained follow-up frames
  while the renderer reports animation pending, so state changes stay visible
  without a blocking wait on the input path.
- Measured input latency (`PreviewHostTest --latency`, gates in `test_all.bat`):
  click ≈ 90 ms avg, key ≈ 95 ms avg, 40-move hover burst settles < 400 ms.
- **Safety is unchanged:** `QUiLoader` wires no user code, so only *built-in*
  widget behaviour runs. Signal/slot `<connections>` and custom-widget Python
  from the `.ui` are never executed; custom widgets stay inert placeholders.

## Layout

| Path | Purpose |
|---|---|
| `src/` | C++ COM handler, class factory, named-pipe render client |
| `renderer/ui_render.py` | QUiLoader load + fit + rasterise + live interactive session |
| `renderer/render_server.py` | named-pipe server wrapping `ui_render` (render + input) |
| `tests/test_render.py` | renderer unit tests incl. code-exec tripwire + interactivity |
| `tests/host.cpp` | COM test host: real HWND, every interface, `--interactive`, `--latency` |
| `tools/build.bat` | vcvars64 + CMake/Ninja Release build |
| `tools/register.py` / `unregister.py` | HKCU install / removal (`--check` to read back) |
| `tools/test_all.bat` | renderer + direct host + interactive host + prevhost surrogate |
| `tools/make_payload.py` | builds the trimmed relocatable runtime under `payload/` |
| `installer.nsi` | NSIS script: offline per-user setup + uninstaller |
| `tools/build_installer.bat` | payload + `makensis` in one command |
| `tools/roundtrip_test.py` | silent install → COM + interactive test → uninstall → clean |
| `dist/` | built `QtUiPreviewHandler-Setup-1.0.0.exe` |
| `samples/` | login.ui, editor.ui, customwidget.ui, broken.ui, evil_widget.py, interactive_edit.ui, interactive_check.ui, large_form.ui |
| `.wheels/` | offline cp314 wheels (PySide6-Essentials, shiboken6, pywin32) |

## Build

Requires VS Build Tools 2022 (present) — `tools/build.bat` calls `vcvars64.bat`,
then CMake + Ninja. Static CRT (`/MT`) so the DLL needs no VC redist inside
prevhost. Python side lives in `.venv` (PySide6-Essentials 6.12.0, pywin32 312).

```
tools\build.bat
```

## Register / verify / remove

```
.venv\Scripts\python.exe tools\register.py          # HKCU only
.venv\Scripts\python.exe tools\register.py --check  # read back
tools\test_all.bat                                  # full verification
.venv\Scripts\python.exe tools\unregister.py        # reverse everything
```

Then in Explorer: open a folder with `.ui` files, press **Alt+P** (or View →
Preview pane), select a `.ui`. If the pane was already open, restart Explorer
(Task Manager → Windows Explorer → Restart) or sign out/in so the new handler
is picked up.

## Installer (offline, per-user)

A single self-contained `dist\QtUiPreviewHandler-Setup-1.0.0.exe` (~23 MB) that
needs **no admin, no network and no preinstalled Python/Qt**. It bundles a
trimmed relocatable CPython 3.14 + PySide6-Essentials 6.12 runtime (only the
`QUiLoader` dependency closure: 8 Qt DLLs + 5 bindings + platform/style/image
plugins), the native DLL, the renderer and the register/unregister tools.

Build it (payload is re-verified with the staged interpreter before packaging):

```
tools\build_installer.bat
```

Install / remove:

```
dist\QtUiPreviewHandler-Setup-1.0.0.exe                 # wizard, per-user
dist\QtUiPreviewHandler-Setup-1.0.0.exe /S              # silent
dist\QtUiPreviewHandler-Setup-1.0.0.exe /S /D=C:\path   # silent, custom dir
<installdir>\uninstall.exe                              # wizard uninstall
<installdir>\uninstall.exe /S                           # silent uninstall
```

Default install dir is `%LOCALAPPDATA%\QtUiPreviewHandler`. The installer writes
the same HKCU keys as `register.py` but pointed at the installed paths, adds an
Add/Remove Programs entry, and offers an optional "Restart Explorer" checkbox on
the finish page. The uninstaller unregisters, removes the ARP entry and deletes
the install tree. Verify the whole cycle with `tools\roundtrip_test.py`.

## What was verified

- `tests/test_render.py`: 19/19 pass (fit/shrink, no-upscale, tabs, malformed,
  missing, error tile, 12 sequential renders, custom-widget tripwire, plus
  interactive checkbox toggle / line-edit typing / button `clicked` / reset,
  pressed-state visibility, popup lifecycle, scrolling, keyboard traversal).
- `PreviewHostTest --direct`: COM checks pass, screenshots captured.
- `PreviewHostTest --direct --interactive`: synthesised click + typing into the
  view HWND changes the rendered frame (line edit shows typed text; checkbox
  shows the tick) — proves the C++ input pump → pipe → live-session path.
- `PreviewHostTest --latency`: click/key round-trips and a 40-move hover burst
  stay inside the latency gates (see above); run by `tools/test_all.bat`.
- `PreviewHostTest --local` (registered): passes **inside `prevhost.exe`**
  (observed PID hosting the DLL, `pythonw` renderer as its child), form renders.
- **Real Explorer pane**: on a live desktop, `garde.ui` previews at true size;
  combo dropdown opens, an outside press closes it without toggling the widget
  underneath, a row click commits the value, checkboxes toggle and line edits
  accept typing — including Space/Enter activation and Tab traversal.
- `register.py --check`: every key/value reads back correctly.
- Installer round-trip (`tools/roundtrip_test.py`): silent install → files/ARP/
  registration verified → `PreviewHostTest --local` renders **and** accepts
  interactive input through `prevhost` using the **installed** DLL + bundled
  runtime → silent uninstall removes the tree and every registry key → dev
  registration restored.

The surrogate path above is the same activation Explorer uses, and the live
pane behaviour was additionally confirmed by hand in a real Explorer window.

## Limitations

- Interactivity is **built-in widget behaviour only**: no user code, no `.ui`
  signal/slot `<connections>`, no custom-widget logic (placeholders stay inert).
- Keyboard: printable chars, arrows, Tab/Shift+Tab traversal, Backspace, Enter,
  Space (activation completes on key-up), Esc, Home/End, Delete work; Ctrl+letter
  shortcuts (Ctrl+A/C/V…) are not mapped.
- `.qrc` resource images are not compiled/loaded; relative on-disk pixmaps are.
- Unknown/custom widget classes render as empty placeholder boxes (by design).
- First preview pays ~1–2 s Python+Qt startup; the renderer stays warm afterwards
  (idle-exits after 300 s). Queued input is coalesced into one round-trip, so
  very fast typing or mouse bursts apply in order but paint only the newest frame.
- Per-user only (HKCU). Other users / other machines need their own register run.

## Registry written (HKCU)

```
Software\Classes\CLSID\{D4E7A1C2-9B3F-4E8A-8C6D-2F5B9A0E7C31}
    (default)=Qt Designer UI Preview
    AppID={6d2b5079-2f0b-48dd-ab7f-97cec514d30b}      <- x64 prevhost surrogate
    DisableLowILProcessIsolation=1 (DWORD)
    RendererPython / RendererScript (absolute paths)
    InprocServer32\(default)=<dll>, ThreadingModel=Apartment
Software\Classes\SystemFileAssociations\.ui\shellex\{8895b1c6-b41f-4c1c-a562-0d564250836f}
    (default)={D4E7A1C2-…}
Software\Microsoft\Windows\CurrentVersion\PreviewHandlers
    {D4E7A1C2-…}=Qt Designer UI Preview
```

Note `{534A1E02-D58F-44f0-B58B-36CBED287C7C}` is the **WOW/32-bit** surrogate;
using it on x64 loads the wrong-bitness host. The x64 one is
`{6d2b5079-2f0b-48dd-ab7f-97cec514d30b}`.