@echo off
setlocal
cd /d "%~dp0.."
set ROOT=%CD%
echo === renderer unit tests ===
.venv\Scripts\python.exe tests\test_render.py || exit /b 1
echo === COM host, direct DLL load ===
build\PreviewHostTest.exe --direct build\QtUiPreviewHandler.dll --file "%ROOT%\samples\login.ui" --out build\host-test --wait 12 || exit /b 1
echo === COM host, interactive typing (line edit) + live feedback ===
build\PreviewHostTest.exe --direct build\QtUiPreviewHandler.dll --file "%ROOT%\samples\interactive_edit.ui" --out build\host-edit --interactive --feedback --wait 8 || exit /b 1
echo === COM host, interactive click (checkbox) ===
build\PreviewHostTest.exe --direct build\QtUiPreviewHandler.dll --file "%ROOT%\samples\interactive_check.ui" --out build\host-check --interactive --wait 8 || exit /b 1
echo === COM host, real-size scrolling (oversized form) ===
build\PreviewHostTest.exe --direct build\QtUiPreviewHandler.dll --file "%ROOT%\samples\large_form.ui" --out build\host-scroll --scroll --wait 8 || exit /b 1
echo === COM host, Explorer keyboard via TranslateAccelerator ===
build\PreviewHostTest.exe --direct build\QtUiPreviewHandler.dll --file "%ROOT%\samples\interactive_edit.ui" --out build\host-accel --accel --wait 8 || exit /b 1
echo === COM host, input-to-frame latency (click/key/move burst) ===
build\PreviewHostTest.exe --direct build\QtUiPreviewHandler.dll --file "%ROOT%\samples\interactive_check.ui" --out build\host-latency --latency --wait 8 || exit /b 1
echo === COM host, registered via prevhost surrogate ===
build\PreviewHostTest.exe --local --file "%ROOT%\samples\login.ui" --out build\host-test-surrogate --wait 14 || exit /b 1
echo ALL TESTS PASSED