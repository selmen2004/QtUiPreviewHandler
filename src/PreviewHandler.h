#pragma once
#include <windows.h>
#include <shobjidl.h>
#include <ocidl.h>
#include <oleidl.h>
#include <string>
#include <vector>
#include <set>
#include <deque>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <atomic>
#include "RenderClient.h"

// In-proc COM preview handler. Loaded by prevhost.exe (x64 surrogate).
// Deliberately links NO Qt and NO Python: all heavy rendering happens in a
// separate process so a bad .ui can never take down the shell host.
class CPreviewHandler : public IPreviewHandler,
                        public IPreviewHandlerVisuals,
                        public IInitializeWithFile,
                        public IObjectWithSite,
                        public IOleWindow
{
public:
    CPreviewHandler();
    virtual ~CPreviewHandler();

    // IUnknown
    STDMETHOD(QueryInterface)(REFIID riid, void** ppv) override;
    STDMETHOD_(ULONG, AddRef)() override;
    STDMETHOD_(ULONG, Release)() override;

    // IPreviewHandler
    STDMETHOD(SetWindow)(HWND hwnd, const RECT* prc) override;
    STDMETHOD(SetRect)(const RECT* prc) override;
    STDMETHOD(DoPreview)() override;
    STDMETHOD(Unload)() override;
    STDMETHOD(SetFocus)() override;
    STDMETHOD(QueryFocus)(HWND* phwnd) override;
    STDMETHOD(TranslateAccelerator)(MSG* pmsg) override;

    // IPreviewHandlerVisuals
    STDMETHOD(SetBackgroundColor)(COLORREF color) override;
    STDMETHOD(SetTextColor)(COLORREF color) override;
    STDMETHOD(SetFont)(const LOGFONTW* plf) override;

    // IInitializeWithFile
    STDMETHOD(Initialize)(LPCWSTR pszFilePath, DWORD grfMode) override;

    // IObjectWithSite
    STDMETHOD(SetSite)(IUnknown* punkSite) override;
    STDMETHOD(GetSite)(REFIID riid, void** ppv) override;

    // IOleWindow
    STDMETHOD(GetWindow)(HWND* phwnd) override;
    STDMETHOD(ContextSensitiveHelp)(BOOL fEnterMode) override;

private:
    struct RenderResult {
        unsigned long long generation = 0;
        bool ok = false;
        std::wstring error;
        std::vector<unsigned char> png;
        std::vector<unsigned char> raw;   // top-down BGRA pixels (live wire)
        int frameW = 0, frameH = 0;       // pixel extent of raw (meta renderedSize)
        int cursor = 0;   // 0 arrow, 1 ibeam, 2 hand (from renderer meta)
        int contentW = 0, contentH = 0;   // real-size form extent (meta)
        int scrollX = 0, scrollY = 0;     // clamped offset the renderer used
        bool hadInput = false;            // frame applied forwarded input
        bool anim = false;                // a style fade is still running (meta)
    };

    // One ordered command stream (renders + forwarded input) drained by a
    // single pump thread, so input events reach the renderer in arrival order.
    struct Command {
        unsigned long long generation = 0;
        int w = 0, h = 0;
        int scrollX = 0, scrollY = 0;
        std::string inputJson;   // empty = plain render
        bool reset = false;
        bool hadInput = false;   // set by the pump when inputs were coalesced in
    };

    static LRESULT CALLBACK ViewWndProc(HWND, UINT, WPARAM, LPARAM);
    LRESULT HandleMessage(HWND, UINT, WPARAM, LPARAM);
    void CreateView();
    void DestroyView();
    void RequestRender();
    void ForwardInput(const std::string& inputJson);
    void Enqueue(Command cmd);
    void StartPump();
    void StopPump();
    void PumpLoop();
    void ApplyResult(RenderResult* result);
    void Paint(HDC hdc);
    HBITMAP PngToBitmap(const std::vector<unsigned char>& png);
    HBITMAP RawToBitmap(const std::vector<unsigned char>& raw, int w, int h);
    void ScheduleRender(UINT delayMs);
    void ClampScroll();
    void UpdateScrollBars();
    void ScrollBy(int dx, int dy);
    bool ForwardChar(wchar_t ch);

    std::atomic<LONG> m_ref{1};
    HWND m_hwndParent = nullptr;
    HWND m_hwndView = nullptr;
    RECT m_rc{};
    std::wstring m_file;
    bool m_initialized = false;
    bool m_previewing = false;

    COLORREF m_bg = RGB(255, 255, 255);
    COLORREF m_fg = RGB(32, 32, 32);
    LOGFONTW m_font{};
    bool m_hasFont = false;

    IUnknown* m_site = nullptr;

    RenderClient m_client;
    HBITMAP m_bmp = nullptr;
    int m_bmpW = 0, m_bmpH = 0;
    std::wstring m_error;
    bool m_renderPending = false;

    // Real-size scrolling: the form renders at 100%; when it exceeds the pane
    // these track the viewport offset and the full content extent (from meta).
    int m_scrollX = 0, m_scrollY = 0;
    int m_contentW = 0, m_contentH = 0;

    std::mutex m_cs;
    std::set<RenderResult*> m_pending;
    std::atomic<unsigned long long> m_generation{0};
    UINT_PTR m_timer = 0;

    // Ordered command pump: one worker thread drains renders + forwarded input
    // in arrival order so the live Qt session sees events exactly as typed/clicked.
    std::mutex m_qmu;
    std::condition_variable m_qcv;
    std::deque<Command> m_queue;
    std::thread m_pump;
    bool m_pumpRunning = false;

    bool m_resetNext = false;      // first render after Initialize resets the session
    bool m_mouseHeld = false;      // a button is down -> forward drag moves
    wchar_t m_highSurrogate = 0;   // pending UTF-16 high surrogate from WM_CHAR

    // Live feedback: hover forwarding, cursor affordance, caret-blink repaint.
    bool m_hasFocus = false;       // view has keyboard focus -> run the blink timer
    bool m_trackingMouse = false;  // TrackMouseEvent armed for WM_MOUSELEAVE
    int m_lastHoverX = -1, m_lastHoverY = -1;  // coalesce identical hover moves
    std::atomic<int> m_cursorShape{0};         // 0 arrow, 1 ibeam, 2 hand
    UINT_PTR m_blinkTimer = 0;
};