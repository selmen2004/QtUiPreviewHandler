@echo off
rem Restart the Windows shell (desktop + taskbar + Start menu) so the preview
rem pane picks up the newly registered handler. Closes open Explorer windows.
rem The real work lives in restart_shell.ps1 next to this file: it asks winlogon
rem to respawn the shell (terminate with exit code 2) and waits for the tray
rem window instead of racing it with a manual explorer.exe launch.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0restart_shell.ps1"