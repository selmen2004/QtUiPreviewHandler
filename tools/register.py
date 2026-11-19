"""Register the Qt .ui preview handler for the CURRENT USER (no admin needed).

Writes only HKCU keys:
  Software\\Classes\\CLSID\\{CLSID}                 (InprocServer32 + AppID + IL opt-out)
  Software\\Classes\\SystemFileAssociations\\.ui\\shellex\\{8895b1c6-...}
  Software\\Microsoft\\Windows\\CurrentVersion\\PreviewHandlers  (value {CLSID})

Using SystemFileAssociations means the existing .ui ProgId / double-click
association is left untouched. Idempotent. Run unregister.py to reverse.

  python tools/register.py            # install
  python tools/register.py --check    # read back only
"""

from __future__ import annotations

import argparse
import os
import sys
import winreg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLSID = "{D4E7A1C2-9B3F-4E8A-8C6D-2F5B9A0E7C31}"
PREVIEW_SHELLEX = "{8895b1c6-b41f-4c1c-a562-0d564250836f}"
PREVHOST_APPID_X64 = "{6d2b5079-2f0b-48dd-ab7f-97cec514d30b}"
DISPLAY_NAME = "Qt Designer UI Preview"

CLSID_KEY = r"Software\Classes\CLSID" + "\\" + CLSID
SFA_KEY = r"Software\Classes\SystemFileAssociations\.ui\shellex" + "\\" + PREVIEW_SHELLEX
PREVIEWHANDLERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\PreviewHandlers"


def set_value(key, name, value, kind=winreg.REG_SZ):
    winreg.SetValueEx(key, name, 0, kind, value)


def install(dll: str, pythonw: str, script: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLSID_KEY) as k:
        set_value(k, "", DISPLAY_NAME)
        set_value(k, "AppID", PREVHOST_APPID_X64)
        # Run prevhost at medium IL so the renderer child can use %TEMP% etc.
        set_value(k, "DisableLowILProcessIsolation", 1, winreg.REG_DWORD)
        set_value(k, "RendererPython", pythonw)
        set_value(k, "RendererScript", script)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLSID_KEY + r"\InprocServer32") as k:
        set_value(k, "", dll)
        set_value(k, "ThreadingModel", "Apartment")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SFA_KEY) as k:
        set_value(k, "", CLSID)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PREVIEWHANDLERS_KEY) as k:
        set_value(k, CLSID, DISPLAY_NAME)


def check() -> int:
    ok = True
    expectations = [
        (CLSID_KEY, "", DISPLAY_NAME),
        (CLSID_KEY, "AppID", PREVHOST_APPID_X64),
        (CLSID_KEY + r"\InprocServer32", "ThreadingModel", "Apartment"),
        (SFA_KEY, "", CLSID),
        (PREVIEWHANDLERS_KEY, CLSID, DISPLAY_NAME),
    ]
    for path, name, want in expectations:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                got, _ = winreg.QueryValueEx(k, name)
            good = got == want
            ok &= good
            print(f"  {'OK ' if good else 'BAD'} HKCU\\{path} [{name or 'default'}] = {got}")
        except OSError as exc:
            ok = False
            print(f"  BAD HKCU\\{path} [{name or 'default'}] missing ({exc})")
    for name in ("RendererPython", "RendererScript"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLSID_KEY) as k:
                got, _ = winreg.QueryValueEx(k, name)
            exists = os.path.exists(got)
            ok &= exists
            print(f"  {'OK ' if exists else 'BAD'} {name} = {got}")
        except OSError as exc:
            ok = False
            print(f"  BAD {name} missing ({exc})")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLSID_KEY) as k:
            got, kind = winreg.QueryValueEx(k, "DisableLowILProcessIsolation")
        good = kind == winreg.REG_DWORD and got == 1
        ok &= good
        print(f"  {'OK ' if good else 'BAD'} DisableLowILProcessIsolation = {got}")
    except OSError as exc:
        ok = False
        print(f"  BAD DisableLowILProcessIsolation missing ({exc})")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dll", default=os.path.join(ROOT, "build", "QtUiPreviewHandler.dll"))
    parser.add_argument("--pythonw", default=os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe"))
    parser.add_argument("--script", default=os.path.join(ROOT, "renderer", "render_server.py"))
    args = parser.parse_args()

    if args.check:
        return check()

    dll = os.path.abspath(args.dll)
    pythonw = os.path.abspath(args.pythonw)
    script = os.path.abspath(args.script)
    for path in (dll, pythonw, script):
        if not os.path.exists(path):
            print(f"missing required file: {path}")
            return 1
    install(dll, pythonw, script)
    print(f"registered {CLSID} -> {dll}")
    print("note: restart Explorer (or sign out/in) for the preview pane to pick it up")
    return check()


if __name__ == "__main__":
    sys.exit(main())