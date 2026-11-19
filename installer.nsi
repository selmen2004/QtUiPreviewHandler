; Qt UI Preview Handler - per-user, fully offline NSIS installer.
; Bundles a trimmed relocatable CPython + PySide6 runtime and the native x64
; COM preview-handler DLL. No admin rights, no network, no external deps.
;
; Build:  tools\nsis\nsis-3.10\Bin\makensis.exe installer.nsi
; Output: dist\QtUiPreviewHandler-Setup-1.0.0.exe

Unicode true
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"

!define PRODUCT_NAME      "Qt UI Preview Handler"
!define PRODUCT_VERSION   "1.0.0.0"
!define PRODUCT_PUBLISHER "ThonnyTN"
!define PRODUCT_WEB       "https://github.com/ThonnyTN"
!define ARP_KEY           "Software\Microsoft\Windows\CurrentVersion\Uninstall\QtUiPreviewHandler"
!define STATE_KEY         "Software\QtUiPreviewHandler"
!define ESTIMATED_SIZE_KB 88003

Name "${PRODUCT_NAME}"
OutFile "dist\QtUiPreviewHandler-Setup-1.0.0.exe"
InstallDir "$LOCALAPPDATA\QtUiPreviewHandler"
InstallDirRegKey HKCU "${STATE_KEY}" "InstallDir"
RequestExecutionLevel user

VIProductVersion "${PRODUCT_VERSION}"
VIAddVersionKey "ProductName"     "${PRODUCT_NAME}"
VIAddVersionKey "ProductVersion"  "${PRODUCT_VERSION}"
VIAddVersionKey "FileVersion"     "${PRODUCT_VERSION}"
VIAddVersionKey "FileDescription" "Qt Designer .ui Explorer preview-pane handler"
VIAddVersionKey "CompanyName"     "${PRODUCT_PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "Copyright (c) 2026 ${PRODUCT_PUBLISHER}"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\tools\restart_explorer.bat"
!define MUI_FINISHPAGE_RUN_TEXT "Restart Windows Explorer now (closes open Explorer windows)"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!define MUI_FINISHPAGE_NOAUTOCLOSE

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------------------
Section "Install" SecInstall
  SetOutPath "$INSTDIR"
  ; Recursively bundle the verified payload (DLL, renderer, runtime, tools).
  File /r "payload\*.*"

  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${STATE_KEY}" "InstallDir" "$INSTDIR"

  ; Add/Remove Programs entry (per-user).
  WriteRegStr   HKCU "${ARP_KEY}" "DisplayName"          "${PRODUCT_NAME}"
  WriteRegStr   HKCU "${ARP_KEY}" "DisplayVersion"       "1.0.0"
  WriteRegStr   HKCU "${ARP_KEY}" "Publisher"            "${PRODUCT_PUBLISHER}"
  WriteRegStr   HKCU "${ARP_KEY}" "URLInfoAbout"         "${PRODUCT_WEB}"
  WriteRegStr   HKCU "${ARP_KEY}" "InstallLocation"      "$INSTDIR"
  WriteRegStr   HKCU "${ARP_KEY}" "UninstallString"      '"$INSTDIR\uninstall.exe"'
  WriteRegStr   HKCU "${ARP_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${ARP_KEY}" "NoModify"             1
  WriteRegDWORD HKCU "${ARP_KEY}" "NoRepair"             1
  WriteRegDWORD HKCU "${ARP_KEY}" "EstimatedSize"        ${ESTIMATED_SIZE_KB}

  ; Register the handler at the INSTALLED paths using the bundled interpreter.
  DetailPrint "Registering preview handler (HKCU)..."
  ExecWait '"$INSTDIR\runtime\python\python.exe" "$INSTDIR\tools\register.py" --dll "$INSTDIR\QtUiPreviewHandler.dll" --pythonw "$INSTDIR\runtime\python\pythonw.exe" --script "$INSTDIR\renderer\render_server.py"' $0
  ${If} $0 != 0
    DetailPrint "WARNING: registration returned $0"
    MessageBox MB_OK|MB_ICONEXCLAMATION "Registration reported a problem (code $0).$\r$\nThe files were installed, but the preview pane may not work until you run:$\r$\n$INSTDIR\runtime\python\python.exe $INSTDIR\tools\register.py"
  ${Else}
    DetailPrint "Registration OK."
  ${EndIf}

  ; Tell the shell associations changed so new Explorer windows pick it up.
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
SectionEnd

; ---------------------------------------------------------------------------
Section "Uninstall"
  ; Remove the HKCU registration first (uses the bundled interpreter).
  DetailPrint "Unregistering preview handler..."
  ExecWait '"$INSTDIR\runtime\python\python.exe" "$INSTDIR\tools\unregister.py"' $0
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'

  DeleteRegKey HKCU "${ARP_KEY}"
  DeleteRegKey HKCU "${STATE_KEY}"

  ; The uninstaller runs from a temp copy, so the install tree (including
  ; uninstall.exe) can be removed wholesale.
  SetOutPath "$TEMP"
  RMDir /r "$INSTDIR"
SectionEnd