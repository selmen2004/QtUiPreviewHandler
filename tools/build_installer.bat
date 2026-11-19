@echo off
rem Rebuild the trimmed offline payload and compile the NSIS installer.
rem Output: dist\QtUiPreviewHandler-Setup-1.0.0.exe
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe tools\make_payload.py || exit /b 1
tools\nsis\nsis-3.10\Bin\makensis.exe installer.nsi || exit /b 1
echo.
echo Installer: dist\QtUiPreviewHandler-Setup-1.0.0.exe
endlocal