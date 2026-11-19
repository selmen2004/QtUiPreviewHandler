"""End-to-end installer round-trip verification.

Silently installs the built setup to a temp target, verifies the files, the
Add/Remove Programs entry and the HKCU registration, runs the real COM host
test through prevhost against the INSTALLED DLL + bundled runtime, then silently
uninstalls and confirms everything is removed. Always restores the development
registration afterwards so the machine is left in its prior working state.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import winreg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(ROOT, "dist", "QtUiPreviewHandler-Setup-1.0.0.exe")
HOST = os.path.join(ROOT, "build", "PreviewHostTest.exe")
DEV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TARGET = os.path.join(tempfile.gettempdir(), "qtui_roundtrip")
CLSID = "{D4E7A1C2-9B3F-4E8A-8C6D-2F5B9A0E7C31}"
CLSID_KEY = r"Software\Classes\CLSID" + "\\" + CLSID
ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\QtUiPreviewHandler"

fails = []


def ok(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


def key_exists(hive, path):
    try:
        with winreg.OpenKey(hive, path):
            return True
    except OSError:
        return False


def reg_str(hive, path, name):
    with winreg.OpenKey(hive, path) as k:
        return winreg.QueryValueEx(k, name)[0]


def run(args, timeout=240):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def restore_dev():
    print("\nrestoring development registration...")
    rc, out = run([DEV_PY, os.path.join(ROOT, "tools", "register.py")])
    print(out.strip()[-600:])
    ok(rc == 0, "dev registration restored")


def main():
    print("installer :", INSTALLER)
    print("target    :", TARGET)
    ok(os.path.exists(INSTALLER), "installer exe present")
    ok(os.path.exists(HOST), "COM host test present")

    if os.path.isdir(TARGET):
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", TARGET],
                       capture_output=True)
        time.sleep(1)

    try:
        # 1. silent install to the temp target (/D= must be last, unquoted).
        print("\n[1] silent install")
        rc, out = run([INSTALLER, "/S", "/D=" + TARGET], timeout=300)
        ok(rc == 0, f"installer exit code 0 (got {rc})")
        ok(os.path.isdir(TARGET), "install directory created")

        # 2. files landed.
        print("\n[2] installed files")
        for rel in ("QtUiPreviewHandler.dll",
                    r"runtime\python\python.exe",
                    r"runtime\python\pythonw.exe",
                    r"runtime\python\python314.dll",
                    r"renderer\render_server.py",
                    r"renderer\ui_render.py",
                    r"tools\register.py",
                    r"tools\unregister.py",
                    r"tools\restart_explorer.bat",
                    r"tools\restart_shell.ps1",
                    r"samples\login.ui",
                    r"samples\interactive_edit.ui",
                    "uninstall.exe"):
            ok(os.path.exists(os.path.join(TARGET, rel)), "file: " + rel)
        ok(os.path.exists(os.path.join(
            TARGET, r"runtime\python\Lib\site-packages\PySide6\Qt6Core.dll")),
            "bundled Qt6Core.dll present")

        # 3. Add/Remove Programs entry.
        print("\n[3] Add/Remove Programs")
        ok(key_exists(winreg.HKEY_CURRENT_USER, ARP_KEY), "ARP key present")
        if key_exists(winreg.HKEY_CURRENT_USER, ARP_KEY):
            ok(reg_str(winreg.HKEY_CURRENT_USER, ARP_KEY, "DisplayName")
               == "Qt UI Preview Handler", "ARP DisplayName")
            ok(reg_str(winreg.HKEY_CURRENT_USER, ARP_KEY, "InstallLocation")
               == TARGET, "ARP InstallLocation == target")
            ok(os.path.exists(reg_str(winreg.HKEY_CURRENT_USER, ARP_KEY,
                                      "UninstallString").strip('"')),
               "ARP UninstallString points at a real file")

        # 4. registration points at the INSTALLED paths.
        print("\n[4] HKCU registration")
        inst_py = os.path.join(TARGET, r"runtime\python\python.exe")
        rc, out = run([inst_py, os.path.join(TARGET, r"tools\register.py"),
                       "--check"])
        print(out.strip()[-900:])
        ok(rc == 0, "installed register.py --check passes")
        rp = reg_str(winreg.HKEY_CURRENT_USER, CLSID_KEY, "RendererPython")
        rs = reg_str(winreg.HKEY_CURRENT_USER, CLSID_KEY, "RendererScript")
        ok(rp.startswith(TARGET) and os.path.exists(rp),
           "RendererPython under target and exists")
        ok(rs.startswith(TARGET) and os.path.exists(rs),
           "RendererScript under target and exists")
        inproc = reg_str(winreg.HKEY_CURRENT_USER,
                         CLSID_KEY + r"\InprocServer32", "")
        ok(inproc == os.path.join(TARGET, "QtUiPreviewHandler.dll"),
           "InprocServer32 == installed DLL")

        # 5. real COM host test through prevhost using the installed runtime.
        print("\n[5] COM host test (prevhost -> installed DLL -> bundled pythonw)")
        outdir = os.path.join(ROOT, "build", "roundtrip")
        rc, out = run([HOST, "--local",
                       "--file", os.path.join(TARGET, r"samples\login.ui"),
                       "--out", outdir, "--wait", "20"], timeout=120)
        print(out.strip()[-1400:])
        ok(rc == 0 and "HOST TEST PASSED" in out,
           "host test passed against installed runtime")
        ok(os.path.exists(os.path.join(outdir, "preview_initial.png")),
           "initial preview PNG captured")

        # 5b. interactivity through prevhost: click + type into the live session.
        print("\n[5b] interactive host test (prevhost -> installed DLL)")
        rc, out = run([HOST, "--local",
                       "--file", os.path.join(TARGET, r"samples\interactive_edit.ui"),
                       "--out", outdir, "--interactive", "--wait", "12"], timeout=120)
        print(out.strip()[-900:])
        ok(rc == 0 and "HOST TEST PASSED" in out,
           "interactive host test passed against installed runtime")

        # 6. silent uninstall (uninstaller copies itself to temp; poll for removal).
        print("\n[6] silent uninstall")
        subprocess.Popen([os.path.join(TARGET, "uninstall.exe"), "/S"],
                         close_fds=True)
        gone = False
        for _ in range(60):
            time.sleep(1)
            if not os.path.isdir(TARGET):
                gone = True
                break
        ok(gone, "install directory fully removed")
        if not gone:
            leftover = []
            for r, d, f in os.walk(TARGET):
                for x in f:
                    leftover.append(os.path.relpath(os.path.join(r, x), TARGET))
            print("  leftover files:", leftover[:20])

        # 7. registry cleaned.
        print("\n[7] registry cleaned")
        ok(not key_exists(winreg.HKEY_CURRENT_USER, ARP_KEY), "ARP key removed")
        ok(not key_exists(winreg.HKEY_CURRENT_USER, CLSID_KEY), "CLSID key removed")
        ok(not key_exists(winreg.HKEY_CURRENT_USER,
                          r"Software\Classes\SystemFileAssociations\.ui\shellex"
                          + "\\" + "{8895b1c6-b41f-4c1c-a562-0d564250836f}"),
           "SystemFileAssociations shellex leaf removed")
    finally:
        restore_dev()

    print("\n" + ("ROUNDTRIP PASSED" if not fails
                  else f"ROUNDTRIP FAILED ({len(fails)}): " + "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())