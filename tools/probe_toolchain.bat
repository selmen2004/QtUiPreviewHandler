@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
echo === CL ===
cl 2>&1 | findstr /i "Version"
echo === CMAKE ===
cmake --version 2>&1 | findstr /i "version"
echo === NINJA ===
ninja --version 2>&1
echo === RC ===
where rc 2>&1
echo === LINK ===
where link 2>&1
echo === INCLUDE ===
echo %INCLUDE%