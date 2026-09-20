# =====================================================================
#  Dhan Options Dashboard - STOP EVERYTHING
#  Kills the backend + frontend and frees their ports, even if the
#  original windows were closed and processes were left behind (orphans).
#  Just double-click "Stop Dashboard.bat" (or run this file).
# =====================================================================
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "     DHAN OPTIONS DASHBOARD - SHUTTING DOWN" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""

$PORTS = @(8000, 5173, 5174)   # backend, vite, vite-fallback
$killed = @()

# ---------------------------------------------------------------------
# 1. Kill whatever is LISTENING on our ports (by PID), plus the whole
#    process tree, so orphaned children go too.
# ---------------------------------------------------------------------
Write-Host "[1/3] Killing processes on ports $($PORTS -join ', ')..." -ForegroundColor Yellow
foreach ($port in $PORTS) {
    $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($conn in $conns) {
        $procId = $conn.OwningProcess
        if ($procId -and $procId -ne 0 -and $procId -ne 4) {
            # Kill the whole tree (parent + children) with taskkill /T /F
            $out = taskkill /PID $procId /T /F 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Host "      killed PID $procId on port $port" -ForegroundColor Green
                $killed += $procId
            } else {
                Write-Host "      (PID $procId on port $port already gone)" -ForegroundColor Gray
            }
        }
    }
}

# ---------------------------------------------------------------------
# 2. Sweep for stray processes launched by our launcher:
#    uvicorn (python), vite / npm run dev (node), and the console windows.
# ---------------------------------------------------------------------
Write-Host "[2/3] Sweeping for leftover backend / frontend processes..." -ForegroundColor Yellow

$targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $cl = $_.CommandLine
    if (-not $cl) { return $false }
    ($cl -like "*uvicorn*app.main:app*") -or
    ($cl -like "*vite*") -or
    ($cl -like "*npm run dev*") -or
    ($cl -like "*Dhan Dashboard*")
}

foreach ($t in $targets) {
    if ($killed -contains $t.ProcessId) { continue }
    $out = taskkill /PID $t.ProcessId /T /F 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "      killed PID $($t.ProcessId) ($($t.Name))" -ForegroundColor Green
        $killed += $t.ProcessId
    }
}

if ($killed.Count -eq 0) {
    Write-Host "      nothing running - already clean." -ForegroundColor Gray
}

# ---------------------------------------------------------------------
# 3. Verify the ports are actually free now.
# ---------------------------------------------------------------------
Write-Host "[3/3] Verifying ports are free..." -ForegroundColor Yellow
Start-Sleep -Seconds 2

$stillUsed = @()
foreach ($port in $PORTS) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $stillUsed += $port
        Write-Host "      port $port : STILL IN USE (PID $($conn.OwningProcess))" -ForegroundColor Red
    } else {
        Write-Host "      port $port : free" -ForegroundColor Green
    }
}

Write-Host ""
if ($stillUsed.Count -eq 0) {
    Write-Host "  ============================================" -ForegroundColor Green
    Write-Host "     ALL STOPPED - PORTS FREE" -ForegroundColor Green
    Write-Host "  ============================================" -ForegroundColor Green
} else {
    Write-Host "  ============================================" -ForegroundColor Red
    Write-Host "     SOME PORTS STILL BUSY: $($stillUsed -join ', ')" -ForegroundColor Red
    Write-Host "  ============================================" -ForegroundColor Red
    Write-Host "  Try running this again, or close any open" -ForegroundColor Yellow
    Write-Host "  PowerShell windows and re-run." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  Press any key to close this window..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
