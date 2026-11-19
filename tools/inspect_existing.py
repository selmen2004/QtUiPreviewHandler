"""Inspect a pre-existing preview-handler DLL and any current registration.

Read-only. Reports PE imports, embedded strings worth knowing, and every
preview-handler registration visible under HKCU / HKLM.
"""

from __future__ import annotations

import os
import re
import struct
import sys
import winreg

TARGET = r"G:\Program Files\QtPreviewLite\QtPreviewLite.dll"
PREVIEW_SHELLEX = "{8895b1c6-b41f-4c1c-a562-0d564250836f}"


def pe_imports(data: bytes) -> list[str]:
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    arch = {0x8664: "x64", 0x14C: "x86", 0xAA64: "ARM64"}.get(machine, hex(machine))
    print(f"  machine={arch} optional_magic={hex(magic)} sections={nsec}")
    dd = opt + (112 if magic == 0x20B else 96)
    import_rva, import_size = struct.unpack_from("<II", data, dd + 8)
    sections = []
    sec_off = opt + opt_size
    for i in range(nsec):
        o = sec_off + i * 40
        name = data[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, o + 8)
        sections.append((vaddr, vsize, raddr, rsize, name))

    def rva2off(rva: int) -> int | None:
        for vaddr, vsize, raddr, rsize, _ in sections:
            if vaddr <= rva < vaddr + max(vsize, rsize):
                return raddr + (rva - vaddr)
        return None

    if not import_rva:
        return []
    out = []
    off = rva2off(import_rva)
    if off is None:
        return []
    while True:
        ilt, ts, fc, name_rva, iat = struct.unpack_from("<IIIII", data, off)
        if name_rva == 0:
            break
        no = rva2off(name_rva)
        dll = data[no:data.index(b"\0", no)].decode("ascii", "replace")
        out.append(dll)
        off += 20
    return out


def interesting_strings(data: bytes) -> list[str]:
    ascii_hits = re.findall(rb"[\x20-\x7e]{6,}", data)
    utf16 = re.findall(rb"(?:[\x20-\x7e]\x00){6,}", data)
    seen: list[str] = []
    pat = re.compile(
        r"clsid|\.ui|preview|python|Qt[56]|\.dll|\.exe|Software\\|InprocServer|"
        r"AppID|ThreadingModel|shellex|\{[0-9a-fA-F\-]{36}\}|http|error|fail",
        re.I,
    )
    for raw in ascii_hits + [u.decode("utf-16-le", "replace") for u in utf16]:
        s = raw.decode("ascii", "replace") if isinstance(raw, bytes) else raw
        if pat.search(s) and s not in seen:
            seen.append(s)
    return seen


def reg_preview_handlers() -> None:
    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Classes"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes"),
    ]
    for hive, base in roots:
        label = "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM"
        # Every CLSID that declares the preview-handler shellex anywhere.
        for sub in (r"SystemFileAssociations\.ui\shellex" + "\\" + PREVIEW_SHELLEX,
                    r"*\shellex" + "\\" + PREVIEW_SHELLEX):
            try:
                with winreg.OpenKey(hive, base + "\\" + sub) as k:
                    print(f"  {label}\\{sub} = {winreg.QueryValueEx(k, '')[0]}")
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"  {label}\\{sub} -> {exc}")
        try:
            with winreg.OpenKey(hive, base + r"\CLSID") as clsids:
                i = 0
                while True:
                    try:
                        guid = winreg.EnumKey(clsids, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(clsids, guid + r"\InprocServer32") as ip:
                            dll = winreg.QueryValueEx(ip, "")[0]
                    except OSError:
                        continue
                    if isinstance(dll, str) and ("qtpreview" in dll.lower()
                                                 or "preview" in dll.lower()):
                        print(f"  {label}\\CLSID\\{guid} -> {dll}")
        except OSError as exc:
            print(f"  {label} CLSID scan failed: {exc}")
    for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                        (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        path = r"Software\Microsoft\Windows\CurrentVersion\PreviewHandlers"
        try:
            with winreg.OpenKey(hive, path) as k:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    print(f"  {label} PreviewHandlers: {name} = {val}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"  {label} PreviewHandlers -> {exc}")


def main() -> int:
    print(f"=== {TARGET} ===")
    if not os.path.exists(TARGET):
        print("  (absent)")
    else:
        data = open(TARGET, "rb").read()
        print(f"  size={len(data)}")
        imports = pe_imports(data)
        print("  imports:", ", ".join(imports) or "(none)")
        print("  strings:")
        for s in interesting_strings(data)[:60]:
            print("   ", s)
    print("=== current preview-handler registrations ===")
    reg_preview_handlers()
    return 0


if __name__ == "__main__":
    sys.exit(main())