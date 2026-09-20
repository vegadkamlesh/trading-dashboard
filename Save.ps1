# Save.ps1 — one-command backup of your code to the remote (Bitbucket/GitHub).
#
# Solo-user flow: single branch = master, direct commits (no PR / no merge).
# Usage:
#   .\Save.ps1                 -> commits with an auto message
#   .\Save.ps1 "my message"    -> commits with your message
#
# First time only, set your remote (replace URL with your empty repo):
#   git remote add origin https://bitbucket.org/<you>/trading-dashboard.git
#   git push -u origin master
#
# After that, just run:  .\Save.ps1 "what changed"

param(
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Safety: never commit secrets by accident.
$stagedEnv = git status --porcelain | Select-String -Pattern "\.env$|\.sqlite3$|/logs/"
if ($stagedEnv) {
    Write-Host "REFUSING: a secret/ignored file looks staged:" -ForegroundColor Red
    $stagedEnv | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}

if ([string]::IsNullOrWhiteSpace($Message)) {
    $Message = "update: $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
}

git add -A

# Nothing to commit? Just push (in case previous push failed).
if (-not (git status --porcelain)) {
    Write-Host "No local changes. Pushing any pending commits..." -ForegroundColor Yellow
} else {
    git commit -m "$Message"
    if ($LASTEXITCODE -ne 0) { Write-Host "commit failed" -ForegroundColor Red; exit 1 }
}

$remote = git remote
if (-not $remote) {
    Write-Host "`nNo remote configured yet. Add one, then run again:" -ForegroundColor Yellow
    Write-Host "  git remote add origin https://bitbucket.org/<you>/trading-dashboard.git"
    Write-Host "  git push -u origin master"
    exit 0
}

git push origin master
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nSaved & pushed to remote. ✅" -ForegroundColor Green
} else {
    Write-Host "`nPush failed — check your remote URL / login." -ForegroundColor Red
    exit 1
}
