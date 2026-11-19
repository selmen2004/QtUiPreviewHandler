#include "PreviewHandler.h"
#include "guids.h"
#include <gdiplus.h>
#include <shlwapi.h>
#include <objbase.h>
#include <windowsx.h>
#include <mutex>
#include <thread>
#include <cstdio>
#include <cstdlib>
#include <cwchar>
#include <algorithm>

#pragma comment(lib, "gdiplus.lib")
#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "uuid.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "gdi32.lib")

extern LONG g_dllObjects;

namespace {

constexpr UINT WM_APP_RESULT = WM_APP + 1;
constexpr UINT_PTR RENDER_TIMER_ID = 1;
constexpr UINT_PTR BLINK_TIMER_ID = 2;
constexpr UINT BLINK_INTERVAL_MS = 500;
const wchar_t VIEW_CLASS[] = L"QtUiPreviewView";

std::once_flag g_gdiplusOnce;
ULONG_PTR g_gdiplusToken = 0;
bool g_gdiplusOk = false;

void EnsureGdiplus()
{
    std::call_once(g_gdiplusOnce, [] {
        Gdiplus::GdiplusStartupInput input;
        g_gdiplusOk = (Gdiplus::GdiplusStartup(&g_gdiplusToken, &input, nullptr) == Gdiplus::Ok);
    });
}

HMODULE SelfModule()
{
    HMODULE mod = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       reinterpret_cast<LPCWSTR>(&SelfModule), &mod);
    return mod;
}

std::wstring ColorToHex(COLORREF c)
{
    wchar_t buf[16];
    swprintf_s(buf, L"#%02x%02x%02x", GetRValue(c), GetGValue(c), GetBValue(c));
    return buf;
}

const char* BoolJson(bool b) { return b ? "true" : "false"; }

// Mouse event JSON matching renderer/ui_render.py::_mouse.
std::string MouseJson(const char* action, const char* button, int x, int y, WORD keys)
{
    char buf[256];
    sprintf_s(buf,
        "{\"type\":\"mouse\",\"action\":\"%s\",\"button\":\"%s\",\"x\":%d,\"y\":%d,"
        "\"shift\":%s,\"ctrl\":%s,\"alt\":%s}",
        action, button, x, y,
        BoolJson((keys & MK_SHIFT) != 0),
        BoolJson((keys & MK_CONTROL) != 0),
        BoolJson((GetKeyState(VK_MENU) < 0)));
    return buf;
}

// Keyboard modifier flags sampled live (independent of the message's wParam).
std::string KeyModsJson()
{
    char buf[96];
    sprintf_s(buf, "\"shift\":%s,\"ctrl\":%s,\"alt\":%s",
        BoolJson(GetKeyState(VK_SHIFT) < 0),
        BoolJson(GetKeyState(VK_CONTROL) < 0),
        BoolJson(GetKeyState(VK_MENU) < 0));
    return buf;
}

std::string WideToUtf8(const std::wstring& w)
{
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(),
                        s.data(), n, nullptr, nullptr);
    return s;
}

// Pull the renderer's "cursor" affordance out of the meta JSON without a full
// parser (the DLL deliberately stays tiny): 0 arrow, 1 ibeam, 2 hand.
int CursorFromMeta(const std::wstring& metaJson)
{
    std::string j = WideToUtf8(metaJson);
    size_t pos = j.find("\"cursor\"");
    if (pos == std::string::npos) return 0;
    pos = j.find(':', pos);
    if (pos == std::string::npos) return 0;
    pos = j.find('"', pos);
    if (pos == std::string::npos) return 0;
    size_t end = j.find('"', pos + 1);
    if (end == std::string::npos) return 0;
    std::string v = j.substr(pos + 1, end - pos - 1);
    if (v == "ibeam") return 1;
    if (v == "hand") return 2;
    return 0;
}

// Pull a two-int array ("contentSize":[w,h] / "scroll":[x,y]) out of the meta
// JSON without a full parser, mirroring CursorFromMeta's targeted approach.
bool BoolFromMeta(const std::wstring& metaJson, const char* key)
{
    std::string j = WideToUtf8(metaJson);
    std::string needle = std::string("\"") + key + "\":true";
    return j.find(needle) != std::string::npos;
}

void IntPairFromMeta(const std::wstring& metaJson, const char* key, int& a, int& b)
{
    a = b = 0;
    std::string j = WideToUtf8(metaJson);
    std::string needle = std::string("\"") + key + "\"";
    size_t pos = j.find(needle);
    if (pos == std::string::npos) return;
    pos = j.find('[', pos);
    if (pos == std::string::npos) return;
    size_t end = j.find(']', pos);
    if (end == std::string::npos) return;
    std::string inner = j.substr(pos + 1, end - pos - 1);
    size_t comma = inner.find(',');
    if (comma == std::string::npos) return;
    a = atoi(inner.substr(0, comma).c_str());
    b = atoi(inner.substr(comma + 1).c_str());
}

} // namespace

CPreviewHandler::CPreviewHandler()
{
    InterlockedIncrement(&g_dllObjects);
    m_font.lfHeight = -12;
    m_font.lfWeight = FW_NORMAL;
    wcscpy_s(m_font.lfFaceName, L"Segoe UI");
}

CPreviewHandler::~CPreviewHandler()
{
    Unload();
    if (m_site) { m_site->Release(); m_site = nullptr; }
    InterlockedDecrement(&g_dllObjects);
}

STDMETHODIMP CPreviewHandler::QueryInterface(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_IPreviewHandler_local) {
        *ppv = static_cast<IPreviewHandler*>(this);
    } else if (riid == IID_IPreviewHandlerVisuals_local) {
        *ppv = static_cast<IPreviewHandlerVisuals*>(this);
    } else if (riid == IID_IInitializeWithFile_local) {
        *ppv = static_cast<IInitializeWithFile*>(this);
    } else if (riid == IID_IObjectWithSite) {
        *ppv = static_cast<IObjectWithSite*>(this);
    } else if (riid == IID_IOleWindow) {
        *ppv = static_cast<IOleWindow*>(this);
    } else {
        return E_NOINTERFACE;
    }
    AddRef();
    return S_OK;
}

STDMETHODIMP_(ULONG) CPreviewHandler::AddRef()
{
    return (ULONG)m_ref.fetch_add(1) + 1;
}

STDMETHODIMP_(ULONG) CPreviewHandler::Release()
{
    LONG n = m_ref.fetch_sub(1) - 1;
    if (n == 0) delete this;
    return (ULONG)n;
}

STDMETHODIMP CPreviewHandler::Initialize(LPCWSTR pszFilePath, DWORD /*grfMode*/)
{
    if (!pszFilePath) return E_INVALIDARG;
    m_file = pszFilePath;
    m_initialized = true;
    m_resetNext = true;   // fresh live session for this file
    m_scrollX = m_scrollY = 0;
    m_contentW = m_contentH = 0;
    return S_OK;
}

STDMETHODIMP CPreviewHandler::SetWindow(HWND hwnd, const RECT* prc)
{
    if (!hwnd || !prc) return E_INVALIDARG;
    m_hwndParent = hwnd;
    m_rc = *prc;
    if (m_hwndView) {
        SetParent(m_hwndView, hwnd);
        MoveWindow(m_hwndView, m_rc.left, m_rc.top,
                   m_rc.right - m_rc.left, m_rc.bottom - m_rc.top, TRUE);
    }
    return S_OK;
}

STDMETHODIMP CPreviewHandler::SetRect(const RECT* prc)
{
    if (!prc) return E_INVALIDARG;
    m_rc = *prc;
    if (m_hwndView) {
        MoveWindow(m_hwndView, m_rc.left, m_rc.top,
                   m_rc.right - m_rc.left, m_rc.bottom - m_rc.top, TRUE);
        ScheduleRender(120);
    }
    return S_OK;
}

void CPreviewHandler::CreateView()
{
    if (m_hwndView || !m_hwndParent) return;
    static std::once_flag classOnce;
    std::call_once(classOnce, [] {
        WNDCLASSW wc = {};
        wc.lpfnWndProc = &CPreviewHandler::ViewWndProc;
        wc.hInstance = SelfModule();
        wc.lpszClassName = VIEW_CLASS;
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.style = CS_HREDRAW | CS_VREDRAW | CS_DBLCLKS;
        RegisterClassW(&wc);
    });
    m_hwndView = CreateWindowExW(
        0, VIEW_CLASS, L"", WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_VSCROLL | WS_HSCROLL,
        m_rc.left, m_rc.top, m_rc.right - m_rc.left, m_rc.bottom - m_rc.top,
        m_hwndParent, nullptr, SelfModule(), nullptr);
    if (m_hwndView) {
        SetWindowLongPtrW(m_hwndView, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(this));
    }
}

void CPreviewHandler::DestroyView()
{
    if (m_timer && m_hwndView) { KillTimer(m_hwndView, RENDER_TIMER_ID); m_timer = 0; }
    if (m_blinkTimer && m_hwndView) { KillTimer(m_hwndView, BLINK_TIMER_ID); m_blinkTimer = 0; }
    HWND view = m_hwndView;
    if (view) DestroyWindow(view);
    std::lock_guard<std::mutex> lock(m_cs);
    m_hwndView = nullptr;
    for (RenderResult* r : m_pending) delete r;
    m_pending.clear();
}

STDMETHODIMP CPreviewHandler::DoPreview()
{
    if (!m_initialized) return E_UNEXPECTED;
    CreateView();
    if (!m_hwndView) return E_FAIL;
    m_previewing = true;
    StartPump();
    RequestRender();
    return S_OK;
}

STDMETHODIMP CPreviewHandler::Unload()
{
    m_previewing = false;
    DestroyView();
    StopPump();   // drains the queue, unblocks pipe IO, joins the worker
    if (m_bmp) { DeleteObject(m_bmp); m_bmp = nullptr; }
    m_bmpW = m_bmpH = 0;
    m_error.clear();
    m_file.clear();
    m_initialized = false;
    m_mouseHeld = false;
    m_highSurrogate = 0;
    m_hasFocus = false;
    m_trackingMouse = false;
    m_lastHoverX = m_lastHoverY = -1;
    m_cursorShape.store(0);
    m_scrollX = m_scrollY = 0;
    m_contentW = m_contentH = 0;
    m_generation.fetch_add(1);
    return S_OK;
}

STDMETHODIMP CPreviewHandler::SetFocus()
{
    if (!m_hwndView) return E_FAIL;
    ::SetFocus(m_hwndView);
    return S_OK;
}

STDMETHODIMP CPreviewHandler::QueryFocus(HWND* phwnd)
{
    if (!phwnd) return E_POINTER;
    *phwnd = ::GetFocus();
    return S_OK;
}

STDMETHODIMP CPreviewHandler::TranslateAccelerator(MSG* pmsg)
{
    if (!pmsg) return E_INVALIDARG;
    // Explorer / prevhost offer keyboard messages here, but STILL dispatch them
    // to the focused view afterwards. Forwarding here would therefore double
    // every keystroke. Only claim: S_OK stops shell navigation from eating keys
    // the widget consumes; the view receives them via normal WM_KEYDOWN/WM_CHAR
    // dispatch (HandleMessage). Printable key-downs fall through so
    // TranslateMessage still yields WM_CHAR.
    if (m_previewing && m_hwndView && pmsg->hwnd == m_hwndView) {
        if (pmsg->message == WM_CHAR) return S_OK;
        if (pmsg->message == WM_KEYDOWN || pmsg->message == WM_SYSKEYDOWN) {
            unsigned vk = (unsigned)pmsg->wParam;
            bool printable = (vk >= 0x30 && vk <= 0x5A) ||
                             (vk >= 0x60 && vk <= 0x6F) || vk == VK_SPACE;
            return printable ? S_FALSE : S_OK;
        }
    }
    if (m_site) {
        IPreviewHandlerFrame* frame = nullptr;
        if (SUCCEEDED(m_site->QueryInterface(IID_IPreviewHandlerFrame_local,
                                             reinterpret_cast<void**>(&frame))) && frame) {
            HRESULT hr = frame->TranslateAccelerator(pmsg);
            frame->Release();
            return hr;
        }
    }
    return S_FALSE;
}

bool CPreviewHandler::ForwardChar(wchar_t ch)
{
    std::wstring text;
    if (ch >= 0xD800 && ch <= 0xDBFF) { m_highSurrogate = ch; return true; }
    if (ch >= 0xDC00 && ch <= 0xDFFF) {
        if (!m_highSurrogate) return false;
        text.push_back(m_highSurrogate);
        m_highSurrogate = 0;
        text.push_back(ch);
    } else {
        m_highSurrogate = 0;
        if (ch < 0x20) return false;   // control keys arrive via WM_KEYDOWN
        text.push_back(ch);
    }
    std::string json = "{\"type\":\"char\",\"text\":\"" +
                       RenderClient::EscapeJson(text) + "\"," + KeyModsJson() + "}";
    ForwardInput(json);
    return true;
}

STDMETHODIMP CPreviewHandler::SetBackgroundColor(COLORREF color)
{
    m_bg = color;
    if (m_hwndView) InvalidateRect(m_hwndView, nullptr, TRUE);
    return S_OK;
}

STDMETHODIMP CPreviewHandler::SetTextColor(COLORREF color)
{
    m_fg = color;
    if (m_hwndView) InvalidateRect(m_hwndView, nullptr, TRUE);
    return S_OK;
}

STDMETHODIMP CPreviewHandler::SetFont(const LOGFONTW* plf)
{
    if (!plf) return E_INVALIDARG;
    m_font = *plf;
    m_hasFont = true;
    if (m_hwndView) InvalidateRect(m_hwndView, nullptr, TRUE);
    return S_OK;
}

STDMETHODIMP CPreviewHandler::SetSite(IUnknown* punkSite)
{
    if (m_site) { m_site->Release(); m_site = nullptr; }
    if (punkSite) { m_site = punkSite; m_site->AddRef(); }
    return S_OK;
}

STDMETHODIMP CPreviewHandler::GetSite(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (!m_site) return E_FAIL;
    return m_site->QueryInterface(riid, ppv);
}

STDMETHODIMP CPreviewHandler::GetWindow(HWND* phwnd)
{
    if (!phwnd) return E_POINTER;
    *phwnd = m_hwndView ? m_hwndView : m_hwndParent;
    return *phwnd ? S_OK : E_FAIL;
}

STDMETHODIMP CPreviewHandler::ContextSensitiveHelp(BOOL)
{
    return E_NOTIMPL;
}

void CPreviewHandler::ScheduleRender(UINT delayMs)
{
    if (!m_hwndView) return;
    if (m_timer) KillTimer(m_hwndView, RENDER_TIMER_ID);
    m_timer = SetTimer(m_hwndView, RENDER_TIMER_ID, delayMs, nullptr);
}

void CPreviewHandler::ClampScroll()
{
    if (!m_hwndView) { m_scrollX = m_scrollY = 0; return; }
    RECT cr;
    GetClientRect(m_hwndView, &cr);
    int maxX = std::max(0, m_contentW - (int)cr.right);
    int maxY = std::max(0, m_contentH - (int)cr.bottom);
    m_scrollX = std::min(std::max(m_scrollX, 0), maxX);
    m_scrollY = std::min(std::max(m_scrollY, 0), maxY);
}

void CPreviewHandler::UpdateScrollBars()
{
    if (!m_hwndView) return;
    RECT cr;
    GetClientRect(m_hwndView, &cr);
    SCROLLINFO si = {};
    si.cbSize = sizeof(si);
    si.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
    si.nMin = 0;
    // A page >= range hides the bar, so forms that fit show no scrollbars.
    si.nMax = std::max(0, m_contentH - 1);
    si.nPage = (UINT)cr.bottom;
    si.nPos = m_scrollY;
    SetScrollInfo(m_hwndView, SB_VERT, &si, TRUE);
    si.nMax = std::max(0, m_contentW - 1);
    si.nPage = (UINT)cr.right;
    si.nPos = m_scrollX;
    SetScrollInfo(m_hwndView, SB_HORZ, &si, TRUE);
}

void CPreviewHandler::ScrollBy(int dx, int dy)
{
    m_scrollX += dx;
    m_scrollY += dy;
    ClampScroll();
    UpdateScrollBars();
    RequestRender();
}

void CPreviewHandler::RequestRender()
{
    if (!m_hwndView || !m_initialized) return;
    RECT cr;
    GetClientRect(m_hwndView, &cr);
    int w = cr.right, h = cr.bottom;
    if (w < 16 || h < 16) return;
    Command cmd;
    cmd.generation = m_generation.fetch_add(1) + 1;
    cmd.w = w;
    cmd.h = h;
    cmd.scrollX = m_scrollX;
    cmd.scrollY = m_scrollY;
    cmd.reset = m_resetNext;
    m_resetNext = false;
    m_renderPending = true;
    InvalidateRect(m_hwndView, nullptr, TRUE);
    StartPump();
    Enqueue(std::move(cmd));
}

void CPreviewHandler::ForwardInput(const std::string& inputJson)
{
    if (!m_hwndView || !m_initialized || !m_previewing) return;
    RECT cr;
    GetClientRect(m_hwndView, &cr);
    int w = cr.right, h = cr.bottom;
    if (w < 16 || h < 16) return;
    Command cmd;
    cmd.generation = m_generation.fetch_add(1) + 1;
    cmd.w = w;
    cmd.h = h;
    cmd.scrollX = m_scrollX;
    cmd.scrollY = m_scrollY;
    cmd.inputJson = inputJson;
    StartPump();
    Enqueue(std::move(cmd));
}

void CPreviewHandler::Enqueue(Command cmd)
{
    {
        std::lock_guard<std::mutex> lock(m_qmu);
        m_queue.push_back(std::move(cmd));
    }
    m_qcv.notify_one();
}

void CPreviewHandler::StartPump()
{
    std::lock_guard<std::mutex> lock(m_qmu);
    if (m_pumpRunning) return;
    m_pumpRunning = true;
    m_pump = std::thread([this] { PumpLoop(); });
}

void CPreviewHandler::StopPump()
{
    {
        std::lock_guard<std::mutex> lock(m_qmu);
        if (!m_pumpRunning && !m_pump.joinable()) return;
        m_pumpRunning = false;
        m_queue.clear();
    }
    m_qcv.notify_all();
    // Close the pipe so a Request blocked on IO returns promptly; the pump then
    // observes m_pumpRunning == false and exits. Stop() takes no client lock,
    // so this cannot deadlock against an in-flight Request.
    m_client.Stop();
    if (m_pump.joinable()) m_pump.join();
}

void CPreviewHandler::PumpLoop()
{
    for (;;) {
        Command cmd;
        {
            std::unique_lock<std::mutex> lock(m_qmu);
            m_qcv.wait(lock, [this] { return !m_queue.empty() || !m_pumpRunning; });
            if (!m_pumpRunning) return;
            cmd = std::move(m_queue.front());
            m_queue.pop_front();
            // Coalesce bursts: every queued mouse move / key repeat used to cost
            // a full pipe round-trip + raster, so a hover stream drained at
            // ~200 ms per event and feedback lagged seconds behind the pointer.
            // State is cumulative in the renderer, so apply all pending inputs
            // in arrival order in ONE request; geometry/reset/generation come
            // from the newest command. A reset wipes earlier pending inputs.
            std::vector<std::string> inputs;
            if (!cmd.inputJson.empty()) inputs.push_back(cmd.inputJson);
            while (!m_queue.empty()) {
                Command next = std::move(m_queue.front());
                m_queue.pop_front();
                if (next.reset) { cmd.reset = true; inputs.clear(); }
                if (!next.inputJson.empty()) inputs.push_back(next.inputJson);
                cmd.w = next.w;
                cmd.h = next.h;
                cmd.scrollX = next.scrollX;
                cmd.scrollY = next.scrollY;
                cmd.generation = next.generation;
            }
            if (inputs.size() > 1) {
                std::string joined = "[";
                for (size_t i = 0; i < inputs.size(); ++i) {
                    if (i) joined += ",";
                    joined += inputs[i];
                }
                joined += "]";
                cmd.inputJson = std::move(joined);
            }
            cmd.hadInput = !inputs.empty();
        }
        RenderResponse resp;
        m_client.Request(m_file, cmd.w, cmd.h, ColorToHex(m_bg), resp,
                         cmd.inputJson, cmd.reset, cmd.scrollX, cmd.scrollY);
        RenderResult* result = new RenderResult();
        result->generation = cmd.generation;
        result->hadInput = cmd.hadInput;
        result->anim = BoolFromMeta(resp.metaJson, "anim");
        result->ok = resp.ok;
        result->error = resp.error;
        result->png = std::move(resp.png);
        result->raw = std::move(resp.raw);
        IntPairFromMeta(resp.metaJson, "renderedSize", result->frameW, result->frameH);
        result->cursor = CursorFromMeta(resp.metaJson);
        IntPairFromMeta(resp.metaJson, "contentSize", result->contentW, result->contentH);
        IntPairFromMeta(resp.metaJson, "scroll", result->scrollX, result->scrollY);

        std::lock_guard<std::mutex> lock(m_cs);
        if (m_hwndView && cmd.generation == m_generation.load()) {
            m_pending.insert(result);
            PostMessageW(m_hwndView, WM_APP_RESULT, reinterpret_cast<WPARAM>(result), 0);
        } else {
            delete result;   // superseded by a newer render/input; state is cumulative
        }
    }
}

HBITMAP CPreviewHandler::RawToBitmap(const std::vector<unsigned char>& raw, int w, int h)
{
    if (w <= 0 || h <= 0 || raw.size() < (size_t)w * h * 4) return nullptr;
    HDC screen = GetDC(nullptr);
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = w;
    bi.bmiHeader.biHeight = -h;   // top-down, matching the renderer's row order
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    void* bits = nullptr;
    HBITMAP bmp = CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (bmp && bits) memcpy(bits, raw.data(), (size_t)w * h * 4);
    ReleaseDC(nullptr, screen);
    return bmp;
}

HBITMAP CPreviewHandler::PngToBitmap(const std::vector<unsigned char>& png)
{
    if (png.empty()) return nullptr;
    EnsureGdiplus();
    if (!g_gdiplusOk) return nullptr;
    IStream* stream = SHCreateMemStream(png.data(), (UINT)png.size());
    if (!stream) return nullptr;
    HBITMAP bmp = nullptr;
    {
        Gdiplus::Bitmap image(stream);
        if (image.GetLastStatus() == Gdiplus::Ok) {
            image.GetHBITMAP(Gdiplus::Color(GetRValue(m_bg), GetGValue(m_bg), GetBValue(m_bg)), &bmp);
        }
    }
    stream->Release();
    return bmp;
}

void CPreviewHandler::ApplyResult(RenderResult* result)
{
    if (result->generation == m_generation.load()) {
        m_renderPending = false;
        m_cursorShape.store(result->cursor);
        m_contentW = result->contentW;
        m_contentH = result->contentH;
        m_scrollX = result->scrollX;
        m_scrollY = result->scrollY;
        UpdateScrollBars();
        if (result->ok && (!result->png.empty() || !result->raw.empty())) {
            HBITMAP bmp = !result->raw.empty()
                ? RawToBitmap(result->raw, result->frameW, result->frameH)
                : PngToBitmap(result->png);
            if (bmp) {
                if (m_bmp) DeleteObject(m_bmp);
                m_bmp = bmp;
                BITMAP bm = {};
                GetObjectW(bmp, sizeof(bm), &bm);
                m_bmpW = bm.bmWidth;
                m_bmpH = bm.bmHeight;
                m_error.clear();
            } else {
                m_error = L"Could not decode rendered preview.";
            }
        } else {
            m_error = result->error.empty() ? L"Preview unavailable." : result->error;
        }
        if (m_hwndView) InvalidateRect(m_hwndView, nullptr, TRUE);
        // The input frame is painted immediately (press/tick/text are instant);
        // style fade animations settle on a real timer. Chain short follow-up
        // frames until the renderer reports the fade done, instead of blocking
        // the input round-trip on a nested event loop.
        if ((result->hadInput || result->anim) && m_previewing) ScheduleRender(60);
    }
    delete result;
}

void CPreviewHandler::Paint(HDC hdc)
{
    RECT rc;
    GetClientRect(m_hwndView, &rc);
    HBRUSH bg = CreateSolidBrush(m_bg);
    FillRect(hdc, &rc, bg);
    DeleteObject(bg);

    if (m_bmp) {
        HDC mem = CreateCompatibleDC(hdc);
        HGDIOBJ old = SelectObject(mem, m_bmp);
        if (m_bmpW == rc.right && m_bmpH == rc.bottom) {
            BitBlt(hdc, 0, 0, m_bmpW, m_bmpH, mem, 0, 0, SRCCOPY);
        } else {
            SetStretchBltMode(hdc, HALFTONE);
            StretchBlt(hdc, 0, 0, rc.right, rc.bottom, mem, 0, 0, m_bmpW, m_bmpH, SRCCOPY);
        }
        SelectObject(mem, old);
        DeleteDC(mem);
        return;
    }

    const wchar_t* text = m_error.empty()
        ? (m_renderPending ? L"Rendering preview..." : L"")
        : m_error.c_str();
    if (*text) {
        HFONT font = CreateFontIndirectW(&m_font);
        HGDIOBJ old = SelectObject(hdc, font);
        ::SetTextColor(hdc, m_fg);
        SetBkMode(hdc, TRANSPARENT);
        RECT tr = rc;
        InflateRect(&tr, -12, -12);
        DrawTextW(hdc, text, -1, &tr, DT_CENTER | DT_VCENTER | DT_WORDBREAK | DT_END_ELLIPSIS);
        SelectObject(hdc, old);
        DeleteObject(font);
    }
}

LRESULT CALLBACK CPreviewHandler::ViewWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    CPreviewHandler* self = reinterpret_cast<CPreviewHandler*>(
        GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (self) return self->HandleMessage(hwnd, msg, wp, lp);
    return DefWindowProcW(hwnd, msg, wp, lp);
}

LRESULT CPreviewHandler::HandleMessage(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_ERASEBKGND: {
        RECT rc;
        GetClientRect(hwnd, &rc);
        HBRUSH bg = CreateSolidBrush(m_bg);
        FillRect(reinterpret_cast<HDC>(wp), &rc, bg);
        DeleteObject(bg);
        return 1;
    }
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        Paint(hdc);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_SIZE:
        ClampScroll();
        UpdateScrollBars();
        InvalidateRect(hwnd, nullptr, TRUE);
        ScheduleRender(120);
        return 0;
    case WM_TIMER:
        if (wp == RENDER_TIMER_ID) {
            KillTimer(hwnd, RENDER_TIMER_ID);
            m_timer = 0;
            RequestRender();
        } else if (wp == BLINK_TIMER_ID) {
            // While focused, repaint periodically so the caret blinks and hover
            // animations advance even when no input arrives. Skip if a render is
            // already in flight so slow frames cannot backlog the queue.
            if (m_previewing && m_hasFocus && !m_renderPending) RequestRender();
        }
        return 0;
    case WM_APP_RESULT: {
        RenderResult* result = reinterpret_cast<RenderResult*>(wp);
        {
            std::lock_guard<std::mutex> lock(m_cs);
            m_pending.erase(result);
        }
        ApplyResult(result);
        return 0;
    }
    // --- interactive input: forwarded to the live offscreen Qt session ------
    case WM_SETCURSOR:
        if (reinterpret_cast<HWND>(wp) == hwnd && LOWORD(lp) == HTCLIENT) {
            static HCURSOR cursors[3] = {
                LoadCursorW(nullptr, IDC_ARROW),
                LoadCursorW(nullptr, IDC_IBEAM),
                LoadCursorW(nullptr, IDC_HAND),
            };
            int idx = m_cursorShape.load();
            if (idx < 0 || idx > 2) idx = 0;
            SetCursor(cursors[idx]);
            return TRUE;
        }
        return DefWindowProcW(hwnd, msg, wp, lp);
    case WM_GETDLGCODE:
        // Claim every key so Explorer's dialog navigation does not eat arrows/Tab.
        return DLGC_WANTALLKEYS | DLGC_WANTCHARS | DLGC_WANTARROWS;
    case WM_SETFOCUS:
        m_hasFocus = true;
        if (!m_blinkTimer)
            m_blinkTimer = SetTimer(hwnd, BLINK_TIMER_ID, BLINK_INTERVAL_MS, nullptr);
        ForwardInput("{\"type\":\"focus\",\"gained\":true}");
        return 0;
    case WM_KILLFOCUS:
        m_hasFocus = false;
        if (m_blinkTimer) { KillTimer(hwnd, BLINK_TIMER_ID); m_blinkTimer = 0; }
        ForwardInput("{\"type\":\"focus\",\"gained\":false}");
        return 0;
    case WM_LBUTTONDOWN:
    case WM_RBUTTONDOWN:
    case WM_MBUTTONDOWN:
    case WM_LBUTTONDBLCLK: {
        const char* btn = (msg == WM_RBUTTONDOWN) ? "right"
                        : (msg == WM_MBUTTONDOWN) ? "middle" : "left";
        const char* act = (msg == WM_LBUTTONDBLCLK) ? "dblclick" : "press";
        ::SetFocus(hwnd);
        SetCapture(hwnd);
        m_mouseHeld = true;
        ForwardInput(MouseJson(act, btn, GET_X_LPARAM(lp), GET_Y_LPARAM(lp), (WORD)wp));
        return 0;
    }
    case WM_LBUTTONUP:
    case WM_RBUTTONUP:
    case WM_MBUTTONUP: {
        const char* btn = (msg == WM_RBUTTONUP) ? "right"
                        : (msg == WM_MBUTTONUP) ? "middle" : "left";
        m_mouseHeld = false;
        if (GetCapture() == hwnd) ReleaseCapture();
        ForwardInput(MouseJson("release", btn, GET_X_LPARAM(lp), GET_Y_LPARAM(lp), (WORD)wp));
        return 0;
    }
    case WM_MOUSEMOVE: {
        int x = GET_X_LPARAM(lp), y = GET_Y_LPARAM(lp);
        // Forward hover moves too (not just drags) so controls highlight under
        // the pointer; coalesce identical hover coords to limit pipe traffic.
        if (!m_mouseHeld && x == m_lastHoverX && y == m_lastHoverY) return 0;
        m_lastHoverX = x; m_lastHoverY = y;
        const char* btn = (wp & MK_RBUTTON) ? "right"
                        : (wp & MK_MBUTTON) ? "middle" : "left";
        ForwardInput(MouseJson("move", btn, x, y, (WORD)wp));
        if (!m_trackingMouse) {
            TRACKMOUSEEVENT tme = { sizeof(tme), TME_LEAVE, hwnd, 0 };
            TrackMouseEvent(&tme);
            m_trackingMouse = true;
        }
        return 0;
    }
    case WM_MOUSELEAVE:
        m_trackingMouse = false;
        m_lastHoverX = m_lastHoverY = -1;
        ForwardInput("{\"type\":\"mouseleave\"}");
        return 0;
    case WM_MOUSEWHEEL:
    case WM_MOUSEHWHEEL: {
        // When the real-size form overflows the pane, the wheel scrolls the
        // viewport; otherwise it is forwarded to the widget (internal lists etc.).
        RECT cr;
        GetClientRect(hwnd, &cr);
        int delta = GET_WHEEL_DELTA_WPARAM(wp);
        int step = -(delta / 120) * 60;
        bool horiz = (msg == WM_MOUSEHWHEEL) || (GetKeyState(VK_SHIFT) < 0);
        if (horiz && m_contentW > cr.right) { ScrollBy(step, 0); return 0; }
        if (!horiz && m_contentH > cr.bottom) { ScrollBy(0, step); return 0; }
        POINT pt{ GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
        ScreenToClient(hwnd, &pt);
        char buf[256];
        sprintf_s(buf, "{\"type\":\"wheel\",\"delta\":%d,\"x\":%d,\"y\":%d,%s}",
                  delta, pt.x, pt.y, KeyModsJson().c_str());
        ForwardInput(buf);
        return 0;
    }
    case WM_VSCROLL:
    case WM_HSCROLL: {
        RECT cr;
        GetClientRect(hwnd, &cr);
        bool vert = (msg == WM_VSCROLL);
        int& pos = vert ? m_scrollY : m_scrollX;
        int page = vert ? cr.bottom : cr.right;
        int content = vert ? m_contentH : m_contentW;
        int maxPos = std::max(0, content - page);
        switch (LOWORD(wp)) {
        case SB_LINEUP:   pos -= 40; break;
        case SB_LINEDOWN: pos += 40; break;
        case SB_PAGEUP:   pos -= page; break;
        case SB_PAGEDOWN: pos += page; break;
        case SB_TOP:      pos = 0; break;
        case SB_BOTTOM:   pos = maxPos; break;
        case SB_THUMBTRACK:
        case SB_THUMBPOSITION: {
            SCROLLINFO si = {};
            si.cbSize = sizeof(si);
            si.fMask = SIF_TRACKPOS;
            GetScrollInfo(hwnd, vert ? SB_VERT : SB_HORZ, &si);
            pos = si.nTrackPos;
            break;
        }
        default: break;
        }
        pos = std::min(std::max(pos, 0), maxPos);
        UpdateScrollBars();
        RequestRender();
        return 0;
    }
    case WM_KEYDOWN:
    case WM_SYSKEYDOWN: {
        char buf[160];
        sprintf_s(buf, "{\"type\":\"key\",\"action\":\"press\",\"key\":%u,%s}",
                  (unsigned)wp, KeyModsJson().c_str());
        ForwardInput(buf);
        return 0;
    }
    case WM_KEYUP:
    case WM_SYSKEYUP: {
        // Releases matter: QAbstractButton activates Space on key-up, and
        // auto-repeat / shortcut state needs the up edge too.
        char buf[160];
        sprintf_s(buf, "{\"type\":\"key\",\"action\":\"release\",\"key\":%u,%s}",
                  (unsigned)wp, KeyModsJson().c_str());
        ForwardInput(buf);
        return 0;
    }
    case WM_CHAR:
        ForwardChar((wchar_t)wp);
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
}