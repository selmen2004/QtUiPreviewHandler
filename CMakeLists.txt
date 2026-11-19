cmake_minimum_required(VERSION 3.20)
project(QtUiPreviewHandler LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Static CRT so the DLL has no VC-redist dependency inside prevhost.exe.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "Must build x64: 64-bit prevhost.exe cannot load a 32-bit handler.")
endif()

add_library(QtUiPreviewHandler SHARED
    src/dllmain.cpp
    src/PreviewHandler.cpp
    src/RenderClient.cpp
    src/exports.def
)
target_include_directories(QtUiPreviewHandler PRIVATE src)
target_link_libraries(QtUiPreviewHandler PRIVATE shlwapi gdiplus ole32 uuid user32 gdi32)
target_compile_definitions(QtUiPreviewHandler PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)

# End-to-end COM test host: instantiates the registered handler into a real HWND.
add_executable(PreviewHostTest tests/host.cpp)
target_include_directories(PreviewHostTest PRIVATE src)
target_link_libraries(PreviewHostTest PRIVATE shlwapi gdiplus ole32 uuid user32 gdi32)
target_compile_definitions(PreviewHostTest PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)