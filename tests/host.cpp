// End-to-end test host for the preview handler, without Explorer.
//
//   PreviewHostTest.exe --direct <dll> --file <form.ui> --out <dir> [--wait 8]
//   PreviewHostTest.exe --file <form.ui> --out <dir>          (uses registration)
//   --interactive  click/type into the live session; --feedback also checks the
//                  hover cursor affordance and (edit forms) the caret blink.
//
// Creates a real top-level HWND, drives IInitializeWithFile / IPreviewHandler /
// IPreviewHandlerVisuals / IOleWindow exactly as prevhost would, pumps a message
// loop, and screenshots the pane to PNG so the result can be inspected.

#include <windows.h>
#include <shobjidl.h>
#include <objbase.h>
#include <gdiplus.h>
#include <shlwapi.h>
#include <initguid.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include "guids.h"

#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "gdiplus.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "uuid.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "gdi32.lib")

static ULONG_PTR g_gdip = 0;
static int g_failures = 0;

static void Check(bool ok, const char* what)
{
    printf("  %s  %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) ++g_failures;
}

static void Pump(DWORD ms)
{
    ULONGLONG deadline = GetTickCount64() + ms;
    MSG msg;
    while (GetTickCount64() < deadline) {
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        Sleep(10);
    }
}

static int GetPngEncoder(CLSID* clsid)
{
    UINT num = 0, size = 0;
    Gdiplus::GetImageEncodersSize(&num, &size);
    std::vector<BYTE> buf(size);
    Gdiplus::GetImageEncoders(num, size, reinterpret_cast<Gdiplus::ImageCodecInfo*>(buf.data()));
    auto* info = reinterpret_cast<Gdiplus::ImageCodecInfo*>(buf.data());
    for (UINT i = 0; i < num; ++i) {
        if (wcscmp(info[i].MimeType, L"image/png") == 0) { *clsid = info[i].Clsid; return 0; }
    }
    return -1;
}

// Screenshot the client area; returns count of distinct sampled colours.
// If hashOut is given, also writes an FNV-1a hash of the sampled pixels so two
// captures can be compared for any visual change (e.g. typed text / a toggle).
static int Capture(HWND hwnd, const wchar_t* path, unsigned long long* hashOut = nullptr)
{
    RECT rc;
    GetClientRect(hwnd, &rc);
    int w = rc.right, h = rc.bottom;
    HDC screen = GetDC(hwnd);
    HDC mem = CreateCompatibleDC(screen);
    HBITMAP bmp = CreateCompatibleBitmap(screen, w, h);
    HGDIOBJ old = SelectObject(mem, bmp);
    BitBlt(mem, 0, 0, w, h, screen, 0, 0, SRCCOPY);

    int distinct = 0;
    COLORREF seen[64];
    unsigned long long hash = 1469598103934665603ULL;
    // Dense grid: a small glyph (e.g. a checkbox tick) must not fall between
    // sample points, or two visually different frames would hash identically.
    for (int y = 2; y < h; y += 2) {
        for (int x = 2; x < w; x += 2) {
            COLORREF c = GetPixel(mem, x, y);
            hash = (hash ^ (unsigned long long)c) * 1099511628211ULL;
            bool found = false;
            for (int i = 0; i < distinct; ++i) if (seen[i] == c) { found = true; break; }
            if (!found && distinct < 64) seen[distinct++] = c;
        }
    }
    if (hashOut) *hashOut = hash;

    CLSID png;
    if (GetPngEncoder(&png) == 0) {
        Gdiplus::Bitmap image(bmp, nullptr);
        image.Save(path, &png, nullptr);
    }
    SelectObject(mem, old);
    DeleteObject(bmp);
    DeleteDC(mem);
    ReleaseDC(hwnd, screen);
    return distinct;
}

static LRESULT CALLBACK HostProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    return DefWindowProcW(h, m, w, l);
}

// Full-resolution pixel hash via a DIB section (fast memory loop, no GetPixel).
// The step-2 grid in Capture() can miss a 1 px-wide caret; this catches it so a
// blinking caret registers as a frame change.
static unsigned long long CaptureDenseHash(HWND hwnd)
{
    RECT rc;
    GetClientRect(hwnd, &rc);
    int w = rc.right, h = rc.bottom;
    if (w <= 0 || h <= 0) return 0;
    HDC screen = GetDC(hwnd);
    HDC mem = CreateCompatibleDC(screen);
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = w;
    bi.bmiHeader.biHeight = -h;   // top-down
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    void* bits = nullptr;
    HBITMAP dib = CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    HGDIOBJ old = SelectObject(mem, dib);
    BitBlt(mem, 0, 0, w, h, screen, 0, 0, SRCCOPY);
    unsigned long long hash = 1469598103934665603ULL;
    const unsigned char* p = static_cast<const unsigned char*>(bits);
    for (int i = 0; i < w * h * 4; i += 4) {
        unsigned long long px = p[i] | (p[i + 1] << 8) | (p[i + 2] << 16);
        hash = (hash ^ px) * 1099511628211ULL;
    }
    SelectObject(mem, old);
    DeleteObject(dib);
    DeleteDC(mem);
    ReleaseDC(hwnd, screen);
    return hash;
}

// Take foreground so BlockInput() is permitted (it only works for the foreground
// thread). Attaching to the current foreground thread's input queue bypasses the
// foreground-lock timeout that otherwise rejects SetForegroundWindow.
static void ForceForeground(HWND hwnd)
{
    HWND fg = GetForegroundWindow();
    DWORD fgTid = fg ? GetWindowThreadProcessId(fg, nullptr) : 0;
    DWORD myTid = GetCurrentThreadId();
    bool attached = fgTid && fgTid != myTid;
    if (attached) AttachThreadInput(myTid, fgTid, TRUE);
    SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
    ShowWindow(hwnd, SW_SHOW);
    SetForegroundWindow(hwnd);
    BringWindowToTop(hwnd);
    SetActiveWindow(hwnd);
    if (attached) AttachThreadInput(myTid, fgTid, FALSE);
}

int wmain(int argc, wchar_t** argv)
{
    std::wstring dll, file, outDir;
    DWORD waitMs = 8000;
    DWORD clsctx = CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER;
    bool interactive = false;
    bool feedback = false;
    bool scrollTest = false;
    bool accelTest = false;
    bool latencyTest = false;
    for (int i = 1; i < argc; ++i) {
        if (!wcscmp(argv[i], L"--direct") && i + 1 < argc) dll = argv[++i];
        else if (!wcscmp(argv[i], L"--file") && i + 1 < argc) file = argv[++i];
        else if (!wcscmp(argv[i], L"--out") && i + 1 < argc) outDir = argv[++i];
        else if (!wcscmp(argv[i], L"--wait") && i + 1 < argc) waitMs = (DWORD)_wtoi(argv[++i]) * 1000;
        else if (!wcscmp(argv[i], L"--local")) clsctx = CLSCTX_LOCAL_SERVER;
        else if (!wcscmp(argv[i], L"--inproc")) clsctx = CLSCTX_INPROC_SERVER;
        else if (!wcscmp(argv[i], L"--interactive")) interactive = true;
        else if (!wcscmp(argv[i], L"--feedback")) { feedback = true; interactive = true; }
        else if (!wcscmp(argv[i], L"--scroll")) scrollTest = true;
        else if (!wcscmp(argv[i], L"--accel")) accelTest = true;
        else if (!wcscmp(argv[i], L"--latency")) latencyTest = true;
    }
    if (file.empty() || outDir.empty()) {
        wprintf(L"usage: PreviewHostTest [--direct dll] --file form.ui --out dir [--wait sec]\n");
        return 2;
    }
    CreateDirectoryW(outDir.c_str(), nullptr);

    Gdiplus::GdiplusStartupInput gi;
    Gdiplus::GdiplusStartup(&g_gdip, &gi, nullptr);
    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    Check(SUCCEEDED(hr), "CoInitializeEx STA");

    IPreviewHandler* ph = nullptr;
    if (!dll.empty()) {
        HMODULE mod = LoadLibraryW(dll.c_str());
        Check(mod != nullptr, "LoadLibrary(direct)");
        if (!mod) return 1;
        auto fn = reinterpret_cast<HRESULT(STDAPICALLTYPE*)(REFCLSID, REFIID, void**)>(
            GetProcAddress(mod, "DllGetClassObject"));
        Check(fn != nullptr, "GetProcAddress DllGetClassObject");
        if (!fn) return 1;
        IClassFactory* cf = nullptr;
        hr = fn(CLSID_QtUiPreviewHandler, IID_IClassFactory, reinterpret_cast<void**>(&cf));
        Check(SUCCEEDED(hr) && cf, "DllGetClassObject -> IClassFactory");
        if (cf) {
            hr = cf->CreateInstance(nullptr, IID_IPreviewHandler, reinterpret_cast<void**>(&ph));
            Check(SUCCEEDED(hr) && ph, "CreateInstance -> IPreviewHandler");
            cf->Release();
        }
    } else {
        hr = CoCreateInstance(CLSID_QtUiPreviewHandler, nullptr,
                              clsctx,
                              IID_IPreviewHandler, reinterpret_cast<void**>(&ph));
        Check(SUCCEEDED(hr) && ph, "CoCreateInstance(registered) -> IPreviewHandler");
    }
    if (!ph) { CoUninitialize(); return 1; }

    IInitializeWithFile* init = nullptr;
    IPreviewHandlerVisuals* vis = nullptr;
    IObjectWithSite* site = nullptr;
    IOleWindow* ole = nullptr;
    Check(SUCCEEDED(ph->QueryInterface(IID_IInitializeWithFile_local, (void**)&init)), "QI IInitializeWithFile");
    Check(SUCCEEDED(ph->QueryInterface(IID_IPreviewHandlerVisuals_local, (void**)&vis)), "QI IPreviewHandlerVisuals");
    Check(SUCCEEDED(ph->QueryInterface(IID_IObjectWithSite, (void**)&site)), "QI IObjectWithSite");
    Check(SUCCEEDED(ph->QueryInterface(IID_IOleWindow, (void**)&ole)), "QI IOleWindow");

    WNDCLASSW wc = {};
    wc.lpfnWndProc = HostProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"QtUiPreviewTestHost";
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    RegisterClassW(&wc);
    HWND hwnd = CreateWindowExW(0, wc.lpszClassName, L"Preview host test",
                                WS_OVERLAPPEDWINDOW, 60, 60, 760, 560,
                                nullptr, nullptr, wc.hInstance, nullptr);
    Check(hwnd != nullptr, "create host HWND");
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);
    Pump(200);

    if (vis) {
        Check(SUCCEEDED(vis->SetBackgroundColor(RGB(246, 246, 246))), "SetBackgroundColor");
        Check(SUCCEEDED(vis->SetTextColor(RGB(40, 40, 40))), "SetTextColor");
    }
    if (init) Check(SUCCEEDED(init->Initialize(file.c_str(), STGM_READ)), "Initialize(file)");
    if (site) Check(SUCCEEDED(site->SetSite(nullptr)), "SetSite(null)");

    RECT rc;
    GetClientRect(hwnd, &rc);
    Check(SUCCEEDED(ph->SetWindow(hwnd, &rc)), "SetWindow");
    ULONGLONG t0 = GetTickCount64();
    Check(SUCCEEDED(ph->DoPreview()), "DoPreview");
    Pump(waitMs);
    ULONGLONG elapsed = GetTickCount64() - t0;

    HWND view = nullptr;
    if (ole) {
        HRESULT hrw = ole->GetWindow(&view);
        Check(SUCCEEDED(hrw) && view != nullptr, "IOleWindow::GetWindow");
        Check(view && IsChild(hwnd, view), "preview window is a child of host");
    }

    std::wstring p1 = outDir + L"\\preview_initial.png";
    int d1 = Capture(hwnd, p1.c_str());
    printf("  initial capture: %d distinct colours, %llu ms\n", d1, elapsed);
    Check(d1 > 4, "initial preview has rendered content");

    if (scrollTest && view) {
        // Real-size scrolling: an oversized form must show an active scrollbar,
        // scroll on wheel, and jump to the bottom via the scrollbar.
        SCROLLINFO si = {};
        si.cbSize = sizeof(si);
        si.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
        GetScrollInfo(view, SB_VERT, &si);
        printf("  vscroll: max=%d page=%u pos=%d\n", si.nMax, si.nPage, si.nPos);
        Check(si.nMax + 1 > (int)si.nPage, "vertical scrollbar active for oversized form");
        RECT vrc;
        GetClientRect(view, &vrc);
        int cx = (vrc.left + vrc.right) / 2, cy = (vrc.top + vrc.bottom) / 2;
        unsigned long long sb = CaptureDenseHash(view);
        POINT sc{ cx, cy };
        ClientToScreen(view, &sc);
        SendMessageW(view, WM_MOUSEWHEEL, MAKEWPARAM(0, -360), MAKELPARAM(sc.x, sc.y));
        Pump(1500);
        unsigned long long sa = CaptureDenseHash(view);
        Check(sb != sa, "wheel scrolled the real-size viewport");
        std::wstring pw = outDir + L"\\scroll_after_wheel.png";
        Capture(view, pw.c_str());
        SendMessageW(view, WM_VSCROLL, SB_BOTTOM, 0);
        Pump(1500);
        GetScrollInfo(view, SB_VERT, &si);
        printf("  vscroll bottom: pos=%d (max=%d page=%u)\n", si.nPos, si.nMax, si.nPage);
        Check(si.nPos == si.nMax - (int)si.nPage + 1, "scrollbar thumb reached the bottom");
        unsigned long long sbot = CaptureDenseHash(view);
        Check(sbot != sb, "bottom viewport differs from top");
        std::wstring pb = outDir + L"\\scroll_bottom.png";
        Capture(view, pb.c_str());
    }

    if (accelTest && view) {
        // Explorer keyboard contract: the shell offers key messages through
        // IPreviewHandler::TranslateAccelerator before dispatching them. Drive
        // that entry point exactly as Explorer would (S_OK = consumed, skip
        // dispatch; S_FALSE = fall through to TranslateMessage/DispatchMessage)
        // and prove typing reaches the live widget through it.
        ForceForeground(hwnd);
        SetWindowPos(hwnd, nullptr, 0, 0, 760, 560, SWP_NOMOVE | SWP_NOZORDER);
        Pump(600);
        RECT vrc;
        GetClientRect(view, &vrc);
        int cx = (vrc.left + vrc.right) / 2, cy = (vrc.top + vrc.bottom) / 2;
        LPARAM pt = MAKELPARAM(cx, cy);
        POINT sc{ cx, cy };
        ClientToScreen(view, &sc);
        SetCursorPos(sc.x, sc.y);
        Pump(200);
        SendMessageW(view, WM_LBUTTONDOWN, MK_LBUTTON, pt);
        SendMessageW(view, WM_LBUTTONUP, 0, pt);
        ph->SetFocus();
        Pump(600);
        unsigned long long ab = CaptureDenseHash(view);

        // Explorer-style loop: offer TranslateAccelerator first (claim only), then
        // ALWAYS TranslateMessage/DispatchMessage, because the real shell dispatches
        // to the view regardless of the claim. Input must land exactly once.
        bool charClaimed = false;
        int idx = 0;
        for (const wchar_t* p = L"hello"; *p; ++p, ++idx) {
            MSG m = {};
            m.hwnd = view;
            m.message = WM_CHAR;
            m.wParam = (WPARAM)*p;
            HRESULT hr = ph->TranslateAccelerator(&m);
            if (idx == 0) charClaimed = (hr == S_OK);
            TranslateMessage(&m);
            DispatchMessageW(&m);
            Pump(150);
        }
        Pump(1500);
        unsigned long long aa = CaptureDenseHash(view);
        printf("  accel hashes: before=%llu after=%llu\n", ab, aa);
        Check(charClaimed, "TranslateAccelerator claims WM_CHAR (S_OK)");
        Check(ab != aa, "typing via TranslateAccelerator reached the widget");

        // Printable key-down must fall through so TranslateMessage yields WM_CHAR.
        MSG mk = {};
        mk.hwnd = view;
        mk.message = WM_KEYDOWN;
        mk.wParam = 0x48;   // 'H'
        Check(ph->TranslateAccelerator(&mk) == S_FALSE,
              "printable key-down falls through for TranslateMessage");
        // Navigation keys are claimed so shell navigation cannot eat them.
        mk.wParam = VK_TAB;
        Check(ph->TranslateAccelerator(&mk) == S_OK,
              "TranslateAccelerator claims navigation keys (Tab)");
        std::wstring pac = outDir + L"\\accel_typed.png";
        Capture(view, pac.c_str());
    }

    if (latencyTest && view) {
        // Input-to-frame latency on a checkbox-only form: no caret, so the dense
        // hash is stable between toggles and any change is the forwarded input.
        ForceForeground(hwnd);
        SetWindowPos(hwnd, nullptr, 0, 0, 760, 560, SWP_NOMOVE | SWP_NOZORDER);
        Pump(800);
        RECT vrc;
        GetClientRect(view, &vrc);
        int cx = (vrc.left + vrc.right) / 2;
        int cy = (vrc.top + vrc.bottom) / 2;
        LPARAM pt = MAKELPARAM(cx, cy);
        // Park the physical cursor inside the view but on background (not on the
        // checkbox): on the control it would pre-hover the base frame; outside
        // the view TrackMouseEvent posts a stray WM_MOUSELEAVE mid-burst.
        POINT sc{ 8, 8 };
        ClientToScreen(view, &sc);
        SetCursorPos(sc.x, sc.y);
        ph->SetFocus();
        Pump(400);

        auto NowMs = []() {
            LARGE_INTEGER f, c;
            QueryPerformanceFrequency(&f);
            QueryPerformanceCounter(&c);
            return (double)c.QuadPart * 1000.0 / (double)f.QuadPart;
        };
        // Pump until the painted frame differs from base; -1 on timeout.
        auto WaitChange = [&](unsigned long long base, double timeout) {
            double t0 = NowMs();
            for (;;) {
                MSG msg;
                while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
                    TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
                if (CaptureDenseHash(view) != base) return NowMs() - t0;
                if (NowMs() - t0 > timeout) return -1.0;
                Sleep(1);
            }
        };
        auto Stats = [](const char* tag, double* v, int n) {
            double sum = 0, mx = 0;
            int to = 0;
            for (int i = 0; i < n; ++i) {
                if (v[i] < 0) { ++to; continue; }
                sum += v[i];
                if (v[i] > mx) mx = v[i];
            }
            int ok = n - to;
            printf("  latency %s: avg=%.1f ms max=%.1f ms timeouts=%d/%d\n",
                   tag, ok ? sum / ok : -1.0, mx, to, n);
            return ok ? sum / ok : -1.0;
        };

        // Hover burst FIRST, on the still-unfocused session: once the checkbox
        // has focus the vista style paints no hover glow, and once it is already
        // hovered a further move stream changes nothing. 40 moves with no
        // pumping in between must collapse into one round-trip. The renderer is
        // already warm from the initial preview render.
        unsigned long long b0 = CaptureDenseHash(view);
        for (int i = 0; i < 40; ++i)
            SendMessageW(view, WM_MOUSEMOVE, 0, MAKELPARAM(cx - 20 + i, cy));
        double burst = WaitChange(b0, 6000);
        printf("  latency move-burst(40): %.1f ms\n", burst);
        Pump(600);
        // Leave hover so the click/key bases below start from a calm frame.
        SendMessageW(view, WM_MOUSEMOVE, 0, MAKELPARAM(8, 8));
        Pump(400);

        double clickMs[10], keyMs[10];
        for (int i = 0; i < 10; ++i) {
            unsigned long long b = CaptureDenseHash(view);
            SendMessageW(view, WM_LBUTTONDOWN, MK_LBUTTON, pt);
            SendMessageW(view, WM_LBUTTONUP, 0, pt);
            clickMs[i] = WaitChange(b, 3000);
            Pump(300);   // let the settle follow-up land before the next base
        }
        for (int i = 0; i < 10; ++i) {
            unsigned long long b = CaptureDenseHash(view);
            SendMessageW(view, WM_KEYDOWN, VK_SPACE, 0);
            SendMessageW(view, WM_KEYUP, VK_SPACE, 0);
            keyMs[i] = WaitChange(b, 3000);
            Pump(300);
        }
        // A burst of 40 hover moves with no pumping in between: must collapse
        // into one round-trip, not 40 sequential renders.
        double ca = Stats("click", clickMs, 10);
        double ka = Stats("key", keyMs, 10);
        Check(ca >= 0 && ca < 150, "click feedback avg < 150 ms");
        Check(ka >= 0 && ka < 150, "key feedback avg < 150 ms");
        Check(burst >= 0 && burst < 400, "40-move burst settles < 400 ms (coalesced)");
    }

    if (interactive && view) {
        ForceForeground(hwnd);
        // Settle the pane geometry first: a resize landing mid-click would move
        // the click target and (with capture) synthesize a stray second click.
        SetWindowPos(hwnd, nullptr, 0, 0, 760, 560, SWP_NOMOVE | SWP_NOZORDER);
        Pump(600);

        RECT vrc;
        GetClientRect(view, &vrc);
        int cx = (vrc.left + vrc.right) / 2;
        int cy = (vrc.top + vrc.bottom) / 2;
        LPARAM pt = MAKELPARAM(cx, cy);

        // Park the physical cursor on the pane centre (the line edit) so real
        // hover/leave agree with the synthesized input: TrackMouseEvent's
        // WM_MOUSELEAVE follows the physical cursor, and a cursor parked
        // elsewhere would clear hover after every synthesized move.
        POINT sc{ cx, cy };
        ClientToScreen(view, &sc);
        SetCursorPos(sc.x, sc.y);
        Pump(200);

        unsigned long long hBefore = 0, hAfter = 0;
        std::wstring pb = outDir + L"\\interactive_before.png";
        Capture(view, pb.c_str(), &hBefore);

        // Click the pane centre: with a single-widget form this lands on the
        // widget (line edit gains focus / checkbox toggles).
        SendMessageW(view, WM_LBUTTONDOWN, MK_LBUTTON, pt);
        SendMessageW(view, WM_LBUTTONUP, 0, pt);
        Pump(600);
        for (const wchar_t* p = L"hello"; *p; ++p)
            SendMessageW(view, WM_CHAR, (WPARAM)*p, 0);
        Pump(4000);
        Pump(500);

        std::wstring pa = outDir + L"\\interactive_after.png";
        Capture(view, pa.c_str(), &hAfter);
        printf("  interactive hashes: before=%llu after=%llu\n", hBefore, hAfter);
        Check(hBefore != hAfter, "forwarded click/typing changed the rendered frame");

        HWND focus2 = nullptr;
        Check(SUCCEEDED(ph->QueryFocus(&focus2)), "QueryFocus after interaction");

        // Make sure the pane holds keyboard focus for the feedback checks below.
        ph->SetFocus();
        Pump(400);

        // Live feedback #1: a hover move with NO button held must still reach the
        // renderer, and the returned cursor affordance must drive WM_SETCURSOR.
        // The pane centre is a control (line edit -> I-beam, checkbox/button ->
        // hand), so the cursor must leave the default arrow. Retry briefly: a
        // stray physical mouse leave can transiently reset it.
        HCURSOR wantIbeam = LoadCursorW(nullptr, IDC_IBEAM);
        HCURSOR wantHand = LoadCursorW(nullptr, IDC_HAND);
        bool affordance = false;
        for (int attempt = 0; attempt < 60 && !affordance; ++attempt) {
            // Alternate +/-1 px (still on the control) so the move is not
            // coalesced; this keeps refreshing the hover affordance even if a
            // stray physical WM_MOUSELEAVE transiently resets it to arrow.
            LPARAM hp = MAKELPARAM(cx + (attempt % 2), cy);
            SendMessageW(view, WM_MOUSEMOVE, 0, hp);
            Pump(30);
            SendMessageW(view, WM_SETCURSOR, (WPARAM)view, MAKELPARAM(HTCLIENT, 0));
            HCURSOR cur = GetCursor();
            affordance = (cur == wantIbeam || cur == wantHand);
        }
        Check(affordance,
              "hover sets control cursor (I-beam/hand) via forwarded move + meta + WM_SETCURSOR");

        // Live feedback #2 (edit forms only, --feedback): while focused, the DLL's
        // blink timer enqueues plain repaints, so the caret blinks even with no
        // input. Sample several idle frames (dense full-resolution hash: a 1 px
        // caret falls between Capture()'s step-2 grid) and require two to differ.
        if (feedback) {
            // Re-assert focus immediately before sampling: in a batch run the
            // console can steal foreground between tests, which would clear the
            // DLL's blink timer (WM_KILLFOCUS) and freeze the caret.
            ph->SetFocus();
            Pump(300);
            unsigned long long hb[8] = {};
            for (int i = 0; i < 8; ++i) {
                hb[i] = CaptureDenseHash(view);
                printf("  blink[%d]=%llu\n", i, hb[i]);
                Pump(450);
            }
            bool blinkChanged = false;
            for (int i = 1; i < 8; ++i) if (hb[i] != hb[0]) blinkChanged = true;
            Check(blinkChanged, "focused caret blinks across idle repaints (blink timer)");
        }
    }

    if (!interactive && !scrollTest && !accelTest && !latencyTest) {
        SetWindowPos(hwnd, nullptr, 0, 0, 480, 360, SWP_NOMOVE | SWP_NOZORDER);
        Pump(2500);
        std::wstring p2 = outDir + L"\\preview_resized.png";
        int d2 = Capture(hwnd, p2.c_str());
        printf("  resized capture: %d distinct colours\n", d2);
        Check(d2 > 4, "resized preview has rendered content");
    }

    Check(SUCCEEDED(ph->SetFocus()), "SetFocus");
    HWND focus = nullptr;
    Check(SUCCEEDED(ph->QueryFocus(&focus)), "QueryFocus");
    Check(SUCCEEDED(ph->Unload()), "Unload");

    if (init) init->Release();
    if (vis) vis->Release();
    if (site) site->Release();
    if (ole) ole->Release();
    ph->Release();
    DestroyWindow(hwnd);
    Pump(200);
    CoUninitialize();
    Gdiplus::GdiplusShutdown(g_gdip);

    printf("\n%s (%d failures)\n", g_failures ? "HOST TEST FAILED" : "HOST TEST PASSED", g_failures);
    return g_failures ? 1 : 0;
}