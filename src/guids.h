#pragma once
// Declarations only. Exactly one translation unit (dllmain.cpp) includes
// <initguid.h> before this header so the GUIDs are defined once.

// Our preview handler. Unique to this project; do not reuse the placeholder
// {12345678-...} from the earlier QtPreviewLite attempt.
// {D4E7A1C2-9B3F-4E8A-8C6D-2F5B9A0E7C31}
DEFINE_GUID(CLSID_QtUiPreviewHandler,
    0xd4e7a1c2, 0x9b3f, 0x4e8a, 0x8c, 0x6d, 0x2f, 0x5b, 0x9a, 0x0e, 0x7c, 0x31);

// Windows' own 64-bit Preview Handler Surrogate Host (prevhost.exe).
// NOTE: {534A1E02-D58F-44f0-B58B-36CBED287C7C} is the *WOW* (32-on-64) surrogate;
// using it on x64 would load us into the wrong-bitness host.
// {6D2B5079-2F0B-48DD-AB7F-97CEC514D30B}
DEFINE_GUID(AppID_PrevHost64,
    0x6d2b5079, 0x2f0b, 0x48dd, 0xab, 0x7f, 0x97, 0xce, 0xc5, 0x14, 0xd3, 0x0b);

// IID_IPreviewHandler (also the shellex subkey name used for registration).
// {8895B1C6-B41F-4C1C-A562-0D564250836F}
DEFINE_GUID(IID_IPreviewHandler_local,
    0x8895b1c6, 0xb41f, 0x4c1c, 0xa5, 0x62, 0x0d, 0x56, 0x42, 0x50, 0x83, 0x6f);

// IID_IPreviewHandlerVisuals {196BF9A5-B346-4EF0-AA1E-5DCDB76768B1}
DEFINE_GUID(IID_IPreviewHandlerVisuals_local,
    0x196bf9a5, 0xb346, 0x4ef0, 0xaa, 0x1e, 0x5d, 0xcd, 0xb7, 0x67, 0x68, 0xb1);

// IID_IInitializeWithFile {B7D14566-0509-4CCE-A71F-0A554233BD9B}
DEFINE_GUID(IID_IInitializeWithFile_local,
    0xb7d14566, 0x0509, 0x4cce, 0xa7, 0x1f, 0x0a, 0x55, 0x42, 0x33, 0xbd, 0x9b);

// IID_IPreviewHandlerFrame {FEC87AAF-35F9-447A-ADB7-20234491401A}
DEFINE_GUID(IID_IPreviewHandlerFrame_local,
    0xfec87aaf, 0x35f9, 0x447a, 0xad, 0xb7, 0x20, 0x23, 0x44, 0x91, 0x40, 0x1a);