@echo off
setlocal DisableDelayedExpansion
title Install System Monitor for Windows
py -3 -c "import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0install_windows.py" %*
    goto finished
)
python -c "import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0install_windows.py" %*
    goto finished
)
echo Install 64-bit Python 3.12 or later from https://www.python.org/downloads/windows/
echo Include the Python launcher or add Python to PATH, then run this file again.
pause
exit /b 1
:finished
set "status=%errorlevel%"
echo(
pause
exit /b %status%
