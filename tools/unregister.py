"""Remove every registry value/key written by register.py. HKCU only.

Prunes the SystemFileAssociations\\.ui\\shellex branch only if it is empty, so an
existing .ui association or another handler's shellex entry is never touched.
"""

from __future__ import annotations

import sys
import winreg

CLSID = "{D4E7A1C2-9B3F-4E8A-8C6D-2F5B9A0E7C31}"
PREVIEW_SHELLEX = "{8895b1c6-b41f-4c1c-a562-0d564250836f}"
CLSID_KEY = r"Software\Classes\CLSID" + "\\" + CLSID
SFA_LEAF = r"Software\Classes\SystemFileAssociations\.ui\shellex" + "\\" + PREVIEW_SHELLEX
PREVIEWHANDLERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\PreviewHandlers"


def delete_tree(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                delete_tree(path + "\\" + sub)
    except FileNotFoundError:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        print(f"  removed HKCU\\{path}")
    except OSError as exc:
        print(f"  could not remove HKCU\\{path}: {exc}")


def prune_if_empty(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            nsub = winreg.QueryInfoKey(k)[0]
            nval = winreg.QueryInfoKey(k)[1]
        if nsub == 0 and nval == 0:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            print(f"  pruned empty HKCU\\{path}")
    except OSError:
        pass


def main() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PREVIEWHANDLERS_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, CLSID)
        print("  removed PreviewHandlers value")
    except OSError as exc:
        print(f"  PreviewHandlers value: {exc}")
    delete_tree(SFA_LEAF)
    prune_if_empty(r"Software\Classes\SystemFileAssociations\.ui\shellex")
    prune_if_empty(r"Software\Classes\SystemFileAssociations\.ui")
    delete_tree(CLSID_KEY)
    print("unregistered")
    return 0


if __name__ == "__main__":
    sys.exit(main())