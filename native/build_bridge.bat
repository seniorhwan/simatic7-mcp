@echo off
REM Build 32-bit x86 native COM worker (run from x86 Native Tools Command Prompt for VS)
cl /nologo /O2 /EHsc /MT "%~dp0s7_com_bridge.cpp" ole32.lib oleaut32.lib /Fe:"%~dp0s7_com_bridge.exe"
if %ERRORLEVEL% NEQ 0 (
  echo Build failed. Run this script from the Visual Studio x86 Native Tools Command Prompt.
  exit /b 1
)
echo Successfully built %~dp0s7_com_bridge.exe
