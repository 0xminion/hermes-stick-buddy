<#
.SYNOPSIS
    Hermes Stick Buddy — Windows setup script
    Installs PlatformIO, clones the firmware, and flashes the M5StickC Plus.
.DESCRIPTION
    Run this in PowerShell on your Windows machine with the stick plugged in via USB.
    It will:
      1. Check for Python (install if missing)
      2. Install PlatformIO CLI
      3. Clone the claude-desktop-buddy firmware
      4. Install the CP210x USB driver (if not detected)
      5. Flash the stick
.NOTES
    Prerequisites: Python 3.9+ must be installed (https://python.org)
#>

$ErrorActionPreference = "Stop"
$RepoDir = "$env:USERPROFILE\hermes-stick-buddy"
$FirmwareRepo = "https://github.com/anthropics/claude-desktop-buddy.git"

function Write-Step($msg) {
    Write-Host "`n=== $msg ===" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "  [!] $msg" -ForegroundColor Yellow
}

function Write-Err($msg) {
    Write-Host "  [X] $msg" -ForegroundColor Red
}

# --- 1. Check Python ---
Write-Step "Checking Python"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    Write-Err "Python not found. Install Python 3.9+ from https://python.org"
    Write-Host "  Make sure to check 'Add Python to PATH' during installation."
    exit 1
}
$pyVersion = & $python.Source --version 2>&1
Write-Ok "Python found: $pyVersion at $($python.Source)"
$pyExe = $python.Source

# --- 2. Install PlatformIO ---
Write-Step "Installing PlatformIO CLI"

# Check if pio is already installed
$pio = Get-Command pio -ErrorAction SilentlyContinue
if (-not $pio) {
    # Try the user Scripts directory
    $userPio = "$env:APPDATA\Python\Scripts\pio.exe"
    if (Test-Path $userPio) {
        $pio = Get-Item $userPio
        $pioExe = $userPio
        Write-Ok "PlatformIO found at $pioExe"
    } else {
        Write-Host "  Installing PlatformIO via pip..."
        & $pyExe -m pip install --user platformio 2>&1 | Out-Host
        $pioExe = "$env:APPDATA\Python\Scripts\pio.exe"
        if (-not (Test-Path $pioExe)) {
            # Try alternative location
            $pioExe = "$env:LOCALAPPDATA\Programs\Python\Python3*\Scripts\pio.exe"
            $found = Get-ChildItem $pioExe -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($found) {
                $pioExe = $found.FullName
            } else {
                Write-Err "PlatformIO installation failed. Try manually: pip install --user platformio"
                exit 1
            }
        }
        Write-Ok "PlatformIO installed at $pioExe"
    }
} else {
    $pioExe = $pio.Source
    Write-Ok "PlatformIO found at $pioExe"
}

# --- 3. Clone firmware ---
Write-Step "Cloning firmware repo"
if (Test-Path "$RepoDir\platformio.ini") {
    Write-Warn "Directory $RepoDir already exists. Using existing clone."
    Write-Host "  To re-clone, delete $RepoDir first."
} else {
    & git clone $FirmwareRepo $RepoDir 2>&1 | Out-Host
    if (-not (Test-Path "$RepoDir\platformio.ini")) {
        Write-Err "Clone failed. Check git is installed and network is available."
        exit 1
    }
    Write-Ok "Firmware cloned to $RepoDir"
}

# --- 4. Check USB driver (CP210x) ---
Write-Step "Checking USB driver"
$comPorts = Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%CP210%' OR Name LIKE '%Silicon Labs%' OR Name LIKE '%USB-to-UART%'" -ErrorAction SilentlyContinue
if ($comPorts) {
    foreach ($port in $comPorts) {
        Write-Ok "Found: $($port.Name)"
    }
} else {
    Write-Warn "CP210x USB-to-UART driver not detected."
    Write-Host "  The M5StickC Plus uses a Silicon Labs CP210x chip."
    Write-Host "  Download the driver from:"
    Write-Host "    https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers"
    Write-Host ""
    Write-Host "  After installing the driver, replug the stick and re-run this script."
    Write-Host "  Continuing anyway — PlatformIO may still detect the port..."
}

# --- 5. Install ESP32 platform ---
Write-Step "Installing ESP32 platform for PlatformIO"
& $pioExe platform install espressif32 2>&1 | Out-Host
Write-Ok "ESP32 platform installed"

# --- 6. Detect COM port ---
Write-Step "Detecting COM port"
$serialPorts = Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%CP210%' OR Name LIKE '%COM%'" -ErrorAction SilentlyContinue
$comPort = $null
foreach ($port in $serialPorts) {
    if ($port.Name -match "COM\d+") {
        $comPort = $matches[0]
        Write-Ok "Detected COM port: $comPort ($($port.Name))"
        break
    }
}
if (-not $comPort) {
    Write-Warn "No COM port detected automatically."
    Write-Host "  Check Device Manager → Ports (COM & LPT)"
    Write-Host "  You can flash manually with:"
    Write-Host "    cd $RepoDir"
    Write-Host "    $pioExe run -t upload"
    Write-Host ""
    $continue = Read-Host "  Continue to flash attempt anyway? (y/n)"
    if ($continue -ne "y") {
        Write-Host "  Aborting. Plug in the stick and re-run this script."
        exit 0
    }
}

# --- 7. Flash the stick ---
Write-Step "Flashing the stick"
Set-Location $RepoDir

Write-Host "  Compiling and uploading firmware..."
Write-Host "  This takes 1-3 minutes on first run (downloads toolchain)..."
Write-Host ""

& $pioExe run -t upload 2>&1 | Out-Host

if ($LASTEXITCODE -eq 0) {
    Write-Ok "Firmware uploaded successfully!"
    Write-Host ""
    Write-Host "  The stick should now show the pet animation." -ForegroundColor Green
    Write-Host "  It will display 'No Claude connected' until the BLE daemon connects." -ForegroundColor Green
} else {
    Write-Err "Flash failed (exit code $LASTEXITCODE)"
    Write-Host ""
    Write-Host "  Common issues:"
    Write-Host "    - Stick not in download mode: hold the power button for 2s to wake it"
    Write-Host "    - Wrong COM port: check Device Manager"
    Write-Host "    - Permission: try running PowerShell as Administrator"
    Write-Host ""
    Write-Host "  Try manually: cd $RepoDir && $pioExe run -t upload"
}

# --- 8. Next steps ---
Write-Step "Next steps"
Write-Host "  1. Start the VPS-side server (on your VPS):"
Write-Host "     cd /home/linuxuser/hermes-stick-buddy/server"
Write-Host "     export STICK_BUDDY_TOKEN=*** -c 'import secrets; print(secrets.token_hex(16))')"
Write-Host "     python3 app.py"
Write-Host ""
Write-Host "  2. Expose via Tailscale (on VPS):"
Write-Host "     tailscale serve --https=9120 http://127.0.0.1:9120"
Write-Host ""
Write-Host "  3. Install BLE daemon deps (on this Windows machine):"
Write-Host "     pip install bleak requests pyyaml"
Write-Host ""
Write-Host "  4. Copy ble_central.py to this machine and run:"
Write-Host "     python ble_central.py --url https://YOUR-VPS.tailnet:9120 --token YOUR_TOKEN"
Write-Host ""
Write-Host "  The stick will pair over BLE and start displaying your token usage."