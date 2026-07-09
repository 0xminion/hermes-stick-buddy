@echo off
REM Hermes Stick Buddy — Windows setup (batch wrapper)
REM Run this if PowerShell execution policy blocks .ps1 scripts

setlocal

set REPO_DIR=%USERPROFILE%\hermes-stick-buddy
set FIRMWARE_REPO=https://github.com/anthropics/claude-desktop-buddy.git

echo.
echo === Hermes Stick Buddy Setup ===
echo.

REM Check Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    where python3 >nul 2>&1
    if %errorlevel% neq 0 (
        echo [X] Python not found. Install Python 3.9+ from https://python.org
        echo     Make sure to check "Add Python to PATH" during installation.
        exit /b 1
    )
    set PY=python3
) else (
    set PY=python
)

echo [OK] Python found: %PY%
%PY% --version

REM Install PlatformIO (detect virtualenv)
echo.
echo === Installing PlatformIO ===
REM Check if in virtualenv (--user doesn't work in venv)
%PY% -c "import sys; exit(0 if (hasattr(sys,'real_prefix') or (hasattr(sys,'base_prefix') and sys.base_prefix != sys.prefix)) else 1)"
if %errorlevel% equ 0 (
    echo (Virtualenv detected — installing without --user^)
    %PY% -m pip install platformio
    REM Get venv Scripts dir
    for /f "delims=" %%i in ('%PY% -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'Scripts'))"') do set PIO=%%i\pio.exe
) else (
    %PY% -m pip install --user platformio
    set PIO=%APPDATA%\Python\Scripts\pio.exe
)
if not exist "%PIO%" (
    set PIO=%LOCALAPPDATA%\Programs\Python\Python3*\Scripts\pio.exe
    for %%f in (%PIO%) do set PIO=%%f
)
echo [OK] PlatformIO at: %PIO%

REM Clone firmware
echo.
echo === Cloning firmware ===
if exist "%REPO_DIR%\platformio.ini" (
    echo [!] Directory %REPO_DIR% already exists. Using existing clone.
) else (
    git clone %FIRMWARE_REPO% %REPO_DIR%
    if not exist "%REPO_DIR%\platformio.ini" (
        echo [X] Clone failed
        exit /b 1
    )
    echo [OK] Firmware cloned to %REPO_DIR%
)

REM Install ESP32 platform
echo.
echo === Installing ESP32 platform ===
"%PIO%" platform install espressif32

REM Flash
echo.
echo === Flashing the stick ===
echo This takes 1-3 minutes on first run (downloads toolchain)...
echo Make sure the M5StickC Plus is plugged in via USB.
echo.
pause

cd /d %REPO_DIR%
"%PIO%" run -t upload

if %errorlevel% equ 0 (
    echo.
    echo [OK] Firmware uploaded successfully!
    echo The stick should now show the pet animation.
    echo It will display "No Claude connected" until the BLE daemon connects.
) else (
    echo.
    echo [X] Flash failed. Common issues:
    echo   - Stick not in download mode: hold power button for 2s to wake it
    echo   - Wrong COM port: check Device Manager
    echo   - Try running as Administrator
)

echo.
echo === Next steps ===
echo 1. Start VPS server: cd /home/linuxuser/hermes-stick-buddy/server ^&^& python3 app.py
echo 2. Expose via Tailscale: tailscale serve --https=9120 http://127.0.0.1:9120
echo 3. Install BLE daemon deps: pip install bleak requests pyyaml
echo 4. Run: python ble_central.py --url https://YOUR-VPS.tailnet:9120 --token YOUR_TOKEN

endlocal