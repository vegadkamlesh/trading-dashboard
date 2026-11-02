param([switch]$NoPause)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ports = @(8000, 5173, 5174)
$failed = $false
$stopped = @{}

function Get-DashboardProcesses {
    param($Processes)
    $Processes | Where-Object {
        $command = [string]$_.CommandLine
        $executable = [string]$_.ExecutablePath
        $runtime = $_.Name -in @("python.exe", "pythonw.exe", "node.exe", "cmd.exe", "powershell.exe")
        $inProject = $command.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0
        $backendPython = $executable.StartsWith(
            (Join-Path $root "backend\.venv") + "\", [StringComparison]::OrdinalIgnoreCase)
        $dashboardCommand = $command -match 'uvicorn|vite|npm(?:\.cmd)?\s+run\s+dev|Run Backend\.bat|Run Frontend\.bat|Start-All\.ps1|Dhan Dashboard\.bat'
        $runtime -and $dashboardCommand -and ($inProject -or $backendPython)
    }
}

function Stop-DashboardTree {
    param([int]$ProcessId, $Processes, $Protected)
    if ($Protected.ContainsKey($ProcessId) -or $stopped.ContainsKey($ProcessId) -or $ProcessId -le 4) { return }
    foreach ($child in @($Processes | Where-Object { $_.ParentProcessId -eq $ProcessId })) {
        Stop-DashboardTree -ProcessId $child.ProcessId -Processes $Processes -Protected $Protected
    }
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        $stopped[$ProcessId] = $true
        Write-Host "  Force-stopped PID $ProcessId" -ForegroundColor Green
    } catch {
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
            Write-Warning "Cannot stop PID ${ProcessId}: $($_.Exception.Message)"
        }
    }
}

Write-Host "`nDHAN DASHBOARD - FORCE STOP" -ForegroundColor Cyan
Write-Host "Stops this project's backend/frontend trees only; other apps are preserved."
Write-Warning "Force stop does not square off broker positions or cancel broker orders. Software algo management will stop."

try {
    # Protect this stop shell and its parents (including the invoking terminal).
    $snapshot = @(Get-CimInstance Win32_Process)
    $protected = @{}
    $ancestor = $PID
    while ($ancestor -gt 0 -and -not $protected.ContainsKey($ancestor)) {
        $protected[$ancestor] = $true
        $parent = $snapshot | Where-Object { $_.ProcessId -eq $ancestor } | Select-Object -First 1
        if (-not $parent) { break }
        $ancestor = [int]$parent.ParentProcessId
    }

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $snapshot = @(Get-CimInstance Win32_Process)
        $targets = @(Get-DashboardProcesses -Processes $snapshot |
            Where-Object { -not $protected.ContainsKey([int]$_.ProcessId) })
        if ($targets.Count -eq 0) { break }
        foreach ($target in $targets) {
            Stop-DashboardTree -ProcessId $target.ProcessId -Processes $snapshot -Protected $protected
        }
        Start-Sleep -Milliseconds 500
    }

    $remaining = @(Get-DashboardProcesses -Processes @(Get-CimInstance Win32_Process) |
        Where-Object { -not $protected.ContainsKey([int]$_.ProcessId) })
    if ($remaining.Count) {
        $failed = $true
        Write-Warning "Dashboard processes still running: $($remaining.ProcessId -join ', '). Try Run as administrator if access was denied."
    }

    # Enumerate listeners once; TIME_WAIT connections are not running servers.
    $listeners = @(Get-NetTCPConnection -State Listen)
    foreach ($port in $ports) {
        $owners = @($listeners | Where-Object { $_.LocalPort -eq $port } |
            Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count) {
            $failed = $true
            Write-Warning "Port $port still occupied by PID(s) $($owners -join ', '). Unidentified/unrelated listeners are NOT killed."
        } else {
            Write-Host "  Port ${port}: free" -ForegroundColor Green
        }
    }
    if (-not $failed) {
        Write-Host "Dashboard stopped. Verified ports free. PIDs stopped: $($stopped.Count)." -ForegroundColor Green
    }
} catch {
    $failed = $true
    Write-Error "Shutdown/verification failed: $($_.Exception.Message)" -ErrorAction Continue
}

if (-not $NoPause) {
    Read-Host "Press Enter to close"
}
if ($failed) { exit 1 }
exit 0
