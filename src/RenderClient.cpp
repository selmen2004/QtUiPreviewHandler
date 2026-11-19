#include "RenderClient.h"
#include <shlwapi.h>
#include <sstream>

#pragma comment(lib, "shlwapi.lib")

namespace {

std::wstring Utf8ToWide(const std::string& s)
{
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
    return w;
}

std::string WideToUtf8(const std::wstring& w)
{
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

std::wstring ModuleDir()
{
    HMODULE mod = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       reinterpret_cast<LPCWSTR>(&ModuleDir), &mod);
    wchar_t path[MAX_PATH * 2] = {};
    GetModuleFileNameW(mod, path, MAX_PATH * 2);
    PathRemoveFileSpecW(path);
    return path;
}

bool ReadRegString(HKEY root, const wchar_t* sub, const wchar_t* value, std::wstring& out)
{
    HKEY key = nullptr;
    if (RegOpenKeyExW(root, sub, 0, KEY_READ, &key) != ERROR_SUCCESS) return false;
    wchar_t buf[1024] = {};
    DWORD type = 0, cb = sizeof(buf) - sizeof(wchar_t);
    LONG rc = RegQueryValueExW(key, value, nullptr, &type, reinterpret_cast<BYTE*>(buf), &cb);
    RegCloseKey(key);
    if (rc != ERROR_SUCCESS || (type != REG_SZ && type != REG_EXPAND_SZ)) return false;
    out = buf;
    return true;
}

// Tiny scalar extractors for the fixed response header; avoids a JSON parser.
bool JsonBool(const std::string& json, const char* key, bool& out)
{
    std::string needle = std::string("\"") + key + "\":";
    size_t p = json.find(needle);
    if (p == std::string::npos) return false;
    p += needle.size();
    while (p < json.size() && (json[p] == ' ')) ++p;
    if (json.compare(p, 4, "true") == 0) { out = true; return true; }
    if (json.compare(p, 5, "false") == 0) { out = false; return true; }
    return false;
}

bool JsonInt(const std::string& json, const char* key, long long& out)
{
    std::string needle = std::string("\"") + key + "\":";
    size_t p = json.find(needle);
    if (p == std::string::npos) return false;
    p += needle.size();
    out = _atoi64(json.c_str() + p);
    return true;
}

std::string JsonEscape(const std::wstring& w)
{
    std::string s = WideToUtf8(w);
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n";  break;
        case '\r': out += "\\r";  break;
        case '\t': out += "\\t";  break;
        default:
            if (static_cast<unsigned char>(c) < 0x20) {
                char buf[8];
                sprintf_s(buf, "\\u%04x", c);
                out += buf;
            } else {
                out += c;
            }
        }
    }
    return out;
}

} // namespace

std::string RenderClient::EscapeJson(const std::wstring& w)
{
    return JsonEscape(w);
}

RenderClient::RenderClient() = default;

RenderClient::~RenderClient()
{
    Stop();
}

void RenderClient::Configure(const std::wstring& pythonExe, const std::wstring& scriptPath, int idleSec)
{
    std::lock_guard<std::mutex> lock(m_mu);
    m_python = pythonExe;
    m_script = scriptPath;
    m_idleSec = idleSec;
}

bool RenderClient::Spawn()
{
    // Resolve renderer location: explicit registry override, else relative to DLL.
    if (m_python.empty() || m_script.empty()) {
        const wchar_t* clsidKey =
            L"Software\\Classes\\CLSID\\{D4E7A1C2-9B3F-4E8A-8C6D-2F5B9A0E7C31}";
        std::wstring py, script;
        if (ReadRegString(HKEY_CURRENT_USER, clsidKey, L"RendererPython", py) &&
            ReadRegString(HKEY_CURRENT_USER, clsidKey, L"RendererScript", script)) {
            m_python = py;
            m_script = script;
        } else {
            // Walk up from the DLL looking for the project layout; handles
            // build/, build/Release/ and bin/ equally.
            std::wstring dir = ModuleDir();
            bool found = false;
            for (int i = 0; i < 4 && !found; ++i) {
                std::wstring py = dir + L"\\.venv\\Scripts\\pythonw.exe";
                std::wstring sc = dir + L"\\renderer\\render_server.py";
                if (GetFileAttributesW(py.c_str()) != INVALID_FILE_ATTRIBUTES &&
                    GetFileAttributesW(sc.c_str()) != INVALID_FILE_ATTRIBUTES) {
                    m_python = py;
                    m_script = sc;
                    found = true;
                } else {
                    wchar_t buf[MAX_PATH * 2] = {};
                    wcscpy_s(buf, dir.c_str());
                    if (!PathRemoveFileSpecW(buf)) break;
                    dir = buf;
                }
            }
            if (!found) return false;
        }
    }
    if (GetFileAttributesW(m_python.c_str()) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(m_script.c_str()) == INVALID_FILE_ATTRIBUTES) {
        return false;
    }

    static LONG counter = 0;
    LONG n = InterlockedIncrement(&counter);
    wchar_t name[160];
    swprintf_s(name, L"\\\\.\\pipe\\QtUiPreview.%lu.%ld.%llu",
               GetCurrentProcessId(), n, GetTickCount64());
    m_pipeName = name;

    m_pipe = CreateNamedPipeW(
        m_pipeName.c_str(),
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1, 64 * 1024, 64 * 1024, 0, nullptr);
    if (m_pipe == INVALID_HANDLE_VALUE) return false;

    std::wstring cmd = L"\"" + m_python + L"\" \"" + m_script + L"\" --pipe \"" +
                       m_pipeName + L"\" --idle " + std::to_wstring(m_idleSec);
    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};
    std::vector<wchar_t> cmdBuf(cmd.begin(), cmd.end());
    cmdBuf.push_back(L'\0');
    BOOL ok = CreateProcessW(nullptr, cmdBuf.data(), nullptr, nullptr, FALSE,
                             CREATE_NO_WINDOW | NORMAL_PRIORITY_CLASS,
                             nullptr, nullptr, &si, &pi);
    if (!ok) {
        CloseHandle(m_pipe);
        m_pipe = INVALID_HANDLE_VALUE;
        return false;
    }
    CloseHandle(pi.hThread);
    m_proc = pi.hProcess;
    return true;
}

bool RenderClient::ConnectPipe()
{
    OVERLAPPED ov = {};
    ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!ov.hEvent) return false;
    BOOL ok = ConnectNamedPipe(m_pipe, &ov);
    DWORD err = GetLastError();
    bool connected = false;
    if (ok) {
        connected = true;
    } else if (err == ERROR_PIPE_CONNECTED) {
        connected = true;
    } else if (err == ERROR_IO_PENDING) {
        DWORD wait = WaitForSingleObject(ov.hEvent, 20000);
        DWORD transferred = 0;
        connected = (wait == WAIT_OBJECT_0) &&
                    GetOverlappedResult(m_pipe, &ov, &transferred, FALSE);
    }
    CloseHandle(ov.hEvent);
    return connected;
}

bool RenderClient::EnsureStarted()
{
    if (m_connected && m_pipe != INVALID_HANDLE_VALUE) return true;
    Stop();
    if (!Spawn()) return false;
    if (!ConnectPipe()) {
        Stop();
        return false;
    }
    m_connected = true;
    return true;
}

bool RenderClient::ReadExact(void* buf, DWORD n)
{
    BYTE* p = static_cast<BYTE*>(buf);
    while (n > 0) {
        DWORD got = 0;
        if (!ReadFile(m_pipe, p, n, &got, nullptr) || got == 0) return false;
        p += got;
        n -= got;
    }
    return true;
}

bool RenderClient::WriteExact(const void* buf, DWORD n)
{
    const BYTE* p = static_cast<const BYTE*>(buf);
    while (n > 0) {
        DWORD wrote = 0;
        if (!WriteFile(m_pipe, p, n, &wrote, nullptr) || wrote == 0) return false;
        p += wrote;
        n -= wrote;
    }
    return true;
}

bool RenderClient::ReadU32(DWORD& value)
{
    BYTE b[4];
    if (!ReadExact(b, 4)) return false;
    value = (DWORD(b[0]) << 24) | (DWORD(b[1]) << 16) | (DWORD(b[2]) << 8) | DWORD(b[3]);
    return true;
}

bool RenderClient::ReadBlob(std::string& out, DWORD n)
{
    if (n > 64u * 1024u * 1024u) return false;
    out.resize(n);
    if (n == 0) return true;
    return ReadExact(out.data(), n);
}

bool RenderClient::Request(const std::wstring& uiPath, int width, int height,
                           const std::wstring& background, RenderResponse& out,
                           const std::string& inputJson, bool reset,
                           int scrollX, int scrollY)
{
    std::lock_guard<std::mutex> lock(m_mu);
    out = RenderResponse{};
    if (!EnsureStarted()) {
        out.error = L"Renderer unavailable (pythonw.exe / render_server.py not found or failed to start).";
        return false;
    }

    std::string req = "{\"id\":1,\"path\":\"" + JsonEscape(uiPath) +
                      "\",\"width\":" + std::to_string(width) +
                      ",\"height\":" + std::to_string(height) +
                      ",\"background\":\"" + JsonEscape(background) +
                      "\",\"margin\":8" +
                      ",\"scrollX\":" + std::to_string(scrollX) +
                      ",\"scrollY\":" + std::to_string(scrollY);
    if (reset) req += ",\"reset\":true";
    if (!inputJson.empty()) req += ",\"input\":" + inputJson;
    req += "}";
    DWORD len = (DWORD)req.size();
    BYTE hdr[4] = { BYTE(len >> 24), BYTE(len >> 16), BYTE(len >> 8), BYTE(len) };
    if (!WriteExact(hdr, 4) || !WriteExact(req.data(), len)) {
        out.error = L"Renderer pipe write failed.";
        Stop();
        return false;
    }

    DWORD hlen = 0;
    std::string header;
    if (!ReadU32(hlen) || !ReadBlob(header, hlen)) {
        out.error = L"Renderer pipe read failed.";
        Stop();
        return false;
    }
    bool ok = false;
    long long pngLen = 0, rawLen = 0, errLen = 0, metaLen = 0;
    JsonBool(header, "ok", ok);
    JsonInt(header, "png_len", pngLen);
    JsonInt(header, "raw_len", rawLen);
    JsonInt(header, "err_len", errLen);
    JsonInt(header, "meta_len", metaLen);

    std::string errUtf8, metaUtf8;
    if (!ReadBlob(errUtf8, (DWORD)errLen) || !ReadBlob(metaUtf8, (DWORD)metaLen)) {
        out.error = L"Renderer truncated response.";
        Stop();
        return false;
    }
    out.error = Utf8ToWide(errUtf8);
    out.metaJson = Utf8ToWide(metaUtf8);
    out.png.resize((size_t)pngLen);
    if (pngLen > 0 && !ReadExact(out.png.data(), (DWORD)pngLen)) {
        out.error = L"Renderer truncated PNG.";
        Stop();
        return false;
    }
    out.raw.resize((size_t)rawLen);
    if (rawLen > 0 && !ReadExact(out.raw.data(), (DWORD)rawLen)) {
        out.error = L"Renderer truncated pixel blob.";
        Stop();
        return false;
    }
    out.ok = ok;
    return ok;
}

void RenderClient::Stop()
{
    // Caller holds m_mu, or is the destructor (single-threaded teardown).
    m_connected = false;
    if (m_pipe != INVALID_HANDLE_VALUE) {
        DisconnectNamedPipe(m_pipe);
        CloseHandle(m_pipe);
        m_pipe = INVALID_HANDLE_VALUE;
    }
    if (m_proc != INVALID_HANDLE_VALUE) {
        // Give the renderer a moment to notice the closed pipe, then ensure exit.
        if (WaitForSingleObject(m_proc, 1500) == WAIT_TIMEOUT) {
            TerminateProcess(m_proc, 0);
            WaitForSingleObject(m_proc, 1500);
        }
        CloseHandle(m_proc);
        m_proc = INVALID_HANDLE_VALUE;
    }
}