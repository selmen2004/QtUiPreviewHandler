#include <windows.h>
#include <shobjidl.h>
#include <new>
#include <initguid.h>   // define the GUIDs from guids.h in this TU only
#include "guids.h"
#include "PreviewHandler.h"

LONG g_dllObjects = 0;
LONG g_dllLocks = 0;

namespace {

class CClassFactory : public IClassFactory
{
public:
    CClassFactory() = default;

    STDMETHOD(QueryInterface)(REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) {
            *ppv = static_cast<IClassFactory*>(this);
        } else {
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHOD_(ULONG, AddRef)() override
    {
        return (ULONG)InterlockedIncrement(&m_ref);
    }
    STDMETHOD_(ULONG, Release)() override
    {
        LONG n = InterlockedDecrement(&m_ref);
        if (n == 0) delete this;
        return (ULONG)n;
    }
    STDMETHOD(CreateInstance)(IUnknown* outer, REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (outer) return CLASS_E_NOAGGREGATION;
        CPreviewHandler* handler = new (std::nothrow) CPreviewHandler();
        if (!handler) return E_OUTOFMEMORY;
        HRESULT hr = handler->QueryInterface(riid, ppv);
        handler->Release();
        return hr;
    }
    STDMETHOD(LockServer)(BOOL lock) override
    {
        if (lock) InterlockedIncrement(&g_dllLocks);
        else InterlockedDecrement(&g_dllLocks);
        return S_OK;
    }

private:
    LONG m_ref = 1;
};

} // namespace

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (rclsid != CLSID_QtUiPreviewHandler) return CLASS_E_CLASSNOTAVAILABLE;
    CClassFactory* factory = new (std::nothrow) CClassFactory();
    if (!factory) return E_OUTOFMEMORY;
    HRESULT hr = factory->QueryInterface(riid, ppv);
    factory->Release();
    return hr;
}

STDAPI DllCanUnloadNow()
{
    return (g_dllObjects == 0 && g_dllLocks == 0) ? S_OK : S_FALSE;
}

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID)
{
    // Never do COM/Qt/CRT teardown here: we are under the loader lock.
    if (reason == DLL_PROCESS_DETACH) {
        // nothing
    }
    return TRUE;
}