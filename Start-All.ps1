# =====================================================================
#  Dhan Options Dashboard - ONE-CLICK LAUNCHER
#  Starts the backend + frontend and opens the browser automatically.
#  Just double-click "Dhan Dashboard.bat" (or run this file).
# =====================================================================
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "     DHAN OPTIONS DASHBOARD - STARTING UP" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------
# 0. Make sure Node/npm are on PATH (winget installs need a fresh shell)
# ---------------------------------------------------------------------
$machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath    = [System.Environment]::GetEnvironmentVariable("Path", "User")
$env:Path    = "$machinePath;$userPath"

# ---------------------------------------------------------------------
# 1. Backend setup (venv + deps)  -- only the first time
# ---------------------------------------------------------------------
$backend = Join-Path $root "backend"
Push-Location $backend

if (-not (Test-Path ".\.venv")) {
    Write-Host "[1/4] Creating Python virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
    Write-Host "      Installing backend dependencies..." -ForegroundColor Yellow
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt | Out-Null
} else {
    Write-Host "[1/4] Backend environment already set up." -ForegroundColor Green
}

if (-not (Test-Path ".\.env")) {
    Write-Host ""
    Write-Warning "No backend\.env found!"
    Write-Host "  -> Copy 'backend\env.example.txt' to 'backend\.env' and fill in your Dhan token." -ForegroundColor Yellow
    Write-Host "  -> For an offline UI demo, set:  DHAN_ACCESS_TOKEN=mock-token" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Opening the example file for you. Press Enter here once you've saved '.env'..." -ForegroundColor Cyan
    Start-Process notepad.exe ".\env.example.txt"
    Read-Host
}

Pop-Location

# ---------------------------------------------------------------------
# 2. Frontend setup (npm install)  -- only the first time
# ---------------------------------------------------------------------
$frontend = Join-Path $root "frontend"
Push-Location $frontend
if (-not (Test-Path ".\node_modules")) {
    Write-Host "[2/4] Installing frontend dependencies (first run, may take a minute)..." -ForegroundColor Yellow
    npm install
} else {
    Write-Host "[2/4] Frontend dependencies already installed." -ForegroundColor Green
}
Pop-Location

# ---------------------------------------------------------------------
# 3. Launch backend in its own window
# ---------------------------------------------------------------------
Write-Host "[3/4] Starting backend  ->  http://127.0.0.1:8000" -ForegroundColor Green
$backendCmd = "`$env:PYTHONPATH='.'; Set-Location '$backend'; .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd

# Give the backend a moment to bind the port
Start-Sleep -Seconds 4

# ---------------------------------------------------------------------
# 4. Launch frontend in its own window  + open browser
# ---------------------------------------------------------------------
Write-Host "[4/4] Starting frontend ->  http://127.0.0.1:5173" -ForegroundColor Green
$frontendCmd = "Set-Location '$frontend'; npm run dev"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

# Wait for Vite to boot, then open the browser.
Write-Host ""
Write-Host "     Waiting for the dev server to come up..." -ForegroundColor Cyan
$url = "http://127.0.0.1:5173"
$opened = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $opened = $true; break }
    } catch { }
}

if (-not $opened) {
    # Fall back to the built-in launcher after ~30s anyway.
}

Start-Process $url
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "   DASHBOARD IS RUNNING" -ForegroundColor Green
Write-Host "   Opening: $url" -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Two new windows are running (backend + frontend)." -ForegroundColor Gray
Write-Host "  To STOP: close both of those windows." -ForegroundColor Gray
Write-Host ""
