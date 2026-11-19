"""Assemble a relocatable, offline, size-trimmed runtime payload for the installer.

Layout produced under payload/:
    QtUiPreviewHandler.dll
    renderer/{ui_render,render_server}.py
    runtime/python/...            relocatable CPython (stdlib only) + VC/Qt runtime
    runtime/python/Lib/site-packages/{PySide6(trimmed),shiboken6,win32,...}
    tools/{register,unregister}.py
    tests/test_render.py  samples/*.ui

PySide6 is trimmed to the PE-import dependency closure of the modules QUiLoader
actually needs (QtCore/Gui/Widgets/Xml/UiTools + shiboken), plus the platform,
style and imageformat plugins. No QML, no translations, no unused bindings.

The payload is verified by running the renderer unit tests with the STAGED
interpreter, which proves relocatability before anything is packaged.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(ROOT, "payload")
VENV_SP = os.path.join(ROOT, ".venv", "Lib", "site-packages")
BASE = sys.base_prefix

KEEP_PYD = ["QtCore", "QtGui", "QtWidgets", "QtXml", "QtUiTools"]
STD_LIB_EXCLUDE = {
    "site-packages", "test", "tests", "tkinter", "idlelib", "turtledemo",
    "ensurepip", "distutils", "lib2to3", "__pycache__", "venv",
}
# stdlib C-extensions we don't need (Tk GUI stack); everything else in DLLs/ is kept.
DLLS_SKIP = {"_tkinter.pyd", "tcl90.dll", "tcl9tk90.dll", "python.cat"}
VC_RUNTIME = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
              "msvcp140_codecvt_ids.dll", "vcruntime140.dll",
              "vcruntime140_1.dll", "concrt140.dll")


def pe_imports(path: str) -> list[str]:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    import_rva, _ = struct.unpack_from("<II", data, dd + 8)
    sections = []
    for i in range(nsec):
        o = opt + opt_size + i * 40
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, o + 8)
        sections.append((vaddr, max(vsize, rsize), raddr))

    def rva2off(rva):
        for vaddr, size, raddr in sections:
            if vaddr <= rva < vaddr + size:
                return raddr + (rva - vaddr)
        return None

    if not import_rva:
        return []
    off = rva2off(import_rva)
    if off is None:
        return []
    names = []
    while True:
        fields = struct.unpack_from("<IIIII", data, off)
        name_rva = fields[3]
        if name_rva == 0:
            break
        no = rva2off(name_rva)
        if no is None:
            break
        names.append(data[no:data.index(b"\0", no)].decode("ascii", "replace"))
        off += 20
    return names


def copy_tree(src: str, dst: str, exclude=frozenset()) -> None:
    for r, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in exclude]
        rel = os.path.relpath(r, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out, exist_ok=True)
        for f in files:
            if f.endswith((".pyc",)):
                continue
            shutil.copy2(os.path.join(r, f), os.path.join(out, f))


def build_python(dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for name in ("python.exe", "pythonw.exe", "python314.dll", "python3.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll"):
        src = os.path.join(BASE, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, name))
    # MSVC runtime Qt6 needs; school PCs may lack the VC redist.
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for name in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"):
        src = os.path.join(sys32, name)
        if os.path.exists(src) and not os.path.exists(os.path.join(dst, name)):
            shutil.copy2(src, os.path.join(dst, name))
    copy_tree(os.path.join(BASE, "Lib"), os.path.join(dst, "Lib"), STD_LIB_EXCLUDE)
    # stdlib C-extensions live in DLLs/ in a full Windows install, not Lib/.
    dlls_src = os.path.join(BASE, "DLLs")
    if os.path.isdir(dlls_src):
        dlls_dst = os.path.join(dst, "DLLs")
        os.makedirs(dlls_dst, exist_ok=True)
        for f in os.listdir(dlls_src):
            if f in DLLS_SKIP or f.endswith(".pyc"):
                continue
            shutil.copy2(os.path.join(dlls_src, f), os.path.join(dlls_dst, f))


def trim_pyside6(dst_sp: str) -> None:
    # PySide6 6.12 uses a FLAT layout: Qt6*.dll, *.pyd and pyside6.abi3.dll all
    # sit in the package root; plugins are under PySide6/plugins (no Qt6/bin).
    ps_src = os.path.join(VENV_SP, "PySide6")
    ps_dst = os.path.join(dst_sp, "PySide6")
    os.makedirs(ps_dst, exist_ok=True)

    for f in os.listdir(ps_src):
        if f.endswith(".py") or f == "py.typed":
            shutil.copy2(os.path.join(ps_src, f), os.path.join(ps_dst, f))
    if os.path.isdir(os.path.join(ps_src, "support")):
        copy_tree(os.path.join(ps_src, "support"), os.path.join(ps_dst, "support"))

    for mod in KEEP_PYD:
        shutil.copy2(os.path.join(ps_src, mod + ".pyd"), os.path.join(ps_dst, mod + ".pyd"))
    shutil.copy2(os.path.join(ps_src, "pyside6.abi3.dll"),
                 os.path.join(ps_dst, "pyside6.abi3.dll"))

    # shiboken6 is a sibling package; PySide6 adds its dir via os.add_dll_directory.
    sh_src = os.path.join(VENV_SP, "shiboken6")
    sh_dst = os.path.join(dst_sp, "shiboken6")
    copy_tree(sh_src, sh_dst, {"include", "lib", "__pycache__"})

    # version-matched VC runtime next to the Qt/PySide binaries (school PCs may
    # lack the redist; the System32 copy can be a different build).
    for name in VC_RUNTIME:
        for base in (ps_src, sh_src):
            src = os.path.join(base, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(ps_dst, name))
                break

    have = {f.lower() for f in os.listdir(ps_src) if f.lower().endswith(".dll")}
    plugin_groups = ("platforms", "styles", "imageformats")
    plugins, skipped = [], []
    for g in plugin_groups:
        gd = os.path.join(ps_src, "plugins", g)
        if not os.path.isdir(gd):
            continue
        for f in sorted(os.listdir(gd)):
            if not f.endswith(".dll"):
                continue
            deps = [i for i in pe_imports(os.path.join(gd, f))
                    if i.lower().startswith("qt6")]
            if any(d.lower() not in have for d in deps):
                skipped.append(g + "/" + f)
                continue
            plugins.append((g, f))

    seeds = [os.path.join(ps_dst, m + ".pyd") for m in KEEP_PYD]
    seeds.append(os.path.join(ps_dst, "pyside6.abi3.dll"))
    seeds.append(os.path.join(sh_dst, "shiboken6.abi3.dll"))
    seeds += [os.path.join(ps_src, "plugins", g, f) for g, f in plugins]
    needed: set[str] = set()
    queue = list(seeds)
    while queue:
        cur = queue.pop()
        if not os.path.exists(cur):
            continue
        for imp in pe_imports(cur):
            low = imp.lower()
            if low.startswith("qt6") and low.endswith(".dll") and low not in needed:
                needed.add(low)
                cand = os.path.join(ps_src, imp)
                if os.path.exists(cand):
                    queue.append(cand)
    for dll in sorted(needed):
        shutil.copy2(os.path.join(ps_src, dll), os.path.join(ps_dst, dll))
    for g, f in plugins:
        d = os.path.join(ps_dst, "plugins", g)
        os.makedirs(d, exist_ok=True)
        shutil.copy2(os.path.join(ps_src, "plugins", g, f), os.path.join(d, f))
    print(f"  PySide6 Qt DLL closure: {len(needed)} -> {', '.join(sorted(needed))}")
    if skipped:
        print("  skipped plugins (missing Qt6 dep):", ", ".join(skipped))


def copy_pywin32(dst_sp: str) -> None:
    for name in ("win32", "pywin32_system32"):
        src = os.path.join(VENV_SP, name)
        if os.path.isdir(src):
            copy_tree(src, os.path.join(dst_sp, name))
    for f in os.listdir(VENV_SP):
        if f.endswith(".pth") and "win32" in f.lower():
            shutil.copy2(os.path.join(VENV_SP, f), os.path.join(dst_sp, f))
    # pywin32 bootstrap pth if present
    for f in ("pywin32.pth", "pywin32_bootstrap.pth"):
        src = os.path.join(VENV_SP, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst_sp, f))
    # The .pth's `import pywin32_bootstrap` is a no-op when that module is absent,
    # so guarantee pywintypes/pythoncom resolve by also placing them in the
    # interpreter's application directory (always in the default DLL search path).
    py_root = os.path.dirname(os.path.dirname(os.path.dirname(dst_sp)))
    sys32 = os.path.join(dst_sp, "pywin32_system32")
    if os.path.isdir(sys32):
        for f in os.listdir(sys32):
            if f.lower().endswith(".dll"):
                shutil.copy2(os.path.join(sys32, f), os.path.join(py_root, f))


def main() -> int:
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE)
    print("staging payload...")
    shutil.copy2(os.path.join(ROOT, "build", "QtUiPreviewHandler.dll"),
                 os.path.join(STAGE, "QtUiPreviewHandler.dll"))
    os.makedirs(os.path.join(STAGE, "renderer"))
    for f in ("ui_render.py", "render_server.py"):
        shutil.copy2(os.path.join(ROOT, "renderer", f), os.path.join(STAGE, "renderer", f))
    os.makedirs(os.path.join(STAGE, "tools"))
    for f in ("register.py", "unregister.py", "restart_explorer.bat"):
        shutil.copy2(os.path.join(ROOT, "tools", f), os.path.join(STAGE, "tools", f))
    os.makedirs(os.path.join(STAGE, "tests"))
    shutil.copy2(os.path.join(ROOT, "tests", "test_render.py"), os.path.join(STAGE, "tests", "test_render.py"))
    copy_tree(os.path.join(ROOT, "samples"), os.path.join(STAGE, "samples"), {"__pycache__"})

    py_dst = os.path.join(STAGE, "runtime", "python")
    build_python(py_dst)
    sp_dst = os.path.join(py_dst, "Lib", "site-packages")
    os.makedirs(sp_dst, exist_ok=True)
    trim_pyside6(sp_dst)
    copy_pywin32(sp_dst)

    total = 0
    for r, d, f in os.walk(STAGE):
        for x in f:
            total += os.path.getsize(os.path.join(r, x))
    print(f"payload total: {total/1e6:.1f} MB")

    print("verifying staged interpreter runs the renderer tests...")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.run(
        [os.path.join(py_dst, "python.exe"), os.path.join(STAGE, "tests", "test_render.py")],
        env=env, capture_output=True, text=True)
    print(proc.stdout[-2500:])
    if proc.returncode != 0:
        print(proc.stderr[-2500:])
        print("STAGED RUNTIME FAILED")
        return 1
    proc2 = subprocess.run(
        [os.path.join(py_dst, "python.exe"), "-c",
         "import sys; sys.path.insert(0, r'" + os.path.join(STAGE, "renderer") + "'); import render_server; print('render_server import OK')"],
        env=env, capture_output=True, text=True)
    print(proc2.stdout.strip() or proc2.stderr.strip()[-1500:])
    if proc2.returncode != 0:
        return 1
    # The staged test run leaves build/ artifacts and __pycache__ behind; drop
    # them so the packaged payload is pristine (and smaller).
    shutil.rmtree(os.path.join(STAGE, "build"), ignore_errors=True)
    for r, dirs, files in os.walk(STAGE, topdown=False):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(r, d), ignore_errors=True)
    total = 0
    for r, d, f in os.walk(STAGE):
        for x in f:
            total += os.path.getsize(os.path.join(r, x))
    print(f"clean payload total: {total/1e6:.1f} MB")
    print("PAYLOAD OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())