@echo off
rem Restart Windows Explorer so the preview pane picks up the newly registered
rem handler. Closes any open Explorer windows; offered as an optional checkbox.
taskkill /f /im explorer.exe >nul 2>&1
start "" explorer.exe