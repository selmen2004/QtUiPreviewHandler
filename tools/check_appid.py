"""Read-only: what does the prevhost AppID declare, and where is our DLL loaded?"""
import winreg

GUIDS = {
    "x64": "{6d2b5079-2f0b-48dd-ab7f-97cec514d30b}",
    "wow": "{534A1E02-D58F-44f0-B58B-36CBED287C7C}",
}
for hive, lbl, base in (
    (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE\Classes\AppID"),
    (winreg.HKEY_CURRENT_USER, "HKCU", r"Software\Classes\AppID"),
):
    for tag, g in GUIDS.items():
        path = base + "\\" + g
        try:
            k = winreg.OpenKey(hive, path)
        except OSError:
            print(lbl, tag, "absent")
            continue
        for name in ("", "DllSurrogate", "RunAs"):
            try:
                v, _ = winreg.QueryValueEx(k, name)
                print(lbl, tag, repr(name), "=", repr(v))
            except OSError:
                pass
        winreg.CloseKey(k)