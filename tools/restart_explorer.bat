@echo off
rem Restart the Windows shell (desktop + taskbar + Start menu) so the preview
rem pane picks up the newly registered handler. Closes open Explorer windows.
rem
rem explorer.exe only takes the SHELL role when no shell is running; launched
rem while a tray window still exists (or after winlogon already respawned the
rem shell) it opens a plain folder window instead. So: kill, wait for every
rem explorer process to exit, give winlogon's AutoRestartShell a grace period
rem to respawn the shell itself, and launch explorer.exe only if nothing came
rem back. Never leaves a stray folder window behind.
powershell -NoProfile -Command ^
  "$ErrorActionPreference = 'SilentlyContinue';" ^
  "taskkill /f /im explorer.exe | Out-Null;" ^
  "for ($i = 0; $i -lt 100 -and (Get-Process explorer); $i++) { Start-Sleep -Milliseconds 100 };" ^
  "Start-Sleep -Milliseconds 2000;" ^
  "if (-not (Get-Process explorer)) { Start-Process explorer.exe }"