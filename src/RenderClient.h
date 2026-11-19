#pragma once
#include <windows.h>
#include <string>
#include <vector>
#include <mutex>

struct RenderResponse {
    bool ok = false;
    std::wstring error;
    std::wstring metaJson;
    std::vector<unsigned char> png;
    std::vector<unsigned char> raw;   // top-down BGRA pixels (live wire)
};

// Owns the named pipe and the out-of-process PyQt/QUiLoader renderer.
// All methods are called from worker threads; the mutex serialises requests.
class RenderClient {
public:
    RenderClient();
    ~RenderClient();

    void Configure(const std::wstring& pythonExe, const std::wstring& scriptPath, int idleSec);
    bool EnsureStarted();
    // inputJson: optional raw JSON (object or array) of forwarded input events;
    // reset: drop the renderer's live session for this path/box first.
    bool Request(const std::wstring& uiPath, int width, int height,
                 const std::wstring& background, RenderResponse& out,
                 const std::string& inputJson = std::string(), bool reset = false,
                 int scrollX = 0, int scrollY = 0);
    void Stop();

    static std::string EscapeJson(const std::wstring& w);

private:
    bool Spawn();
    bool ConnectPipe();
    bool ReadExact(void* buf, DWORD n);
    bool WriteExact(const void* buf, DWORD n);
    bool ReadU32(DWORD& value);
    bool ReadBlob(std::string& out, DWORD n);

    std::mutex m_mu;
    HANDLE m_pipe = INVALID_HANDLE_VALUE;
    HANDLE m_proc = INVALID_HANDLE_VALUE;
    std::wstring m_pipeName;
    std::wstring m_python;
    std::wstring m_script;
    int m_idleSec = 300;   // keep the warm renderer alive across reading pauses;
                           // a respawn costs a full Python+Qt startup (~2 s)
    bool m_connected = false;
};