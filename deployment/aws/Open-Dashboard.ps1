[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^i-[0-9a-f]+$')]
    [string]$InstanceId,
    [string]$Profile = 'trading-operator',
    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 8080,
    [string]$AwsExecutable = 'aws',
    [string]$PluginDirectory = ''
)

$ErrorActionPreference = 'Stop'
if ($PluginDirectory) {
    $resolvedPlugin = (Resolve-Path -LiteralPath $PluginDirectory).Path
    $env:PATH = "$resolvedPlugin;$env:PATH"
}
if (-not (Get-Command $AwsExecutable -ErrorAction SilentlyContinue)) {
    throw 'AWS CLI was not found. Install it or pass -AwsExecutable.'
}
if (-not (Get-Command 'session-manager-plugin' -ErrorAction SilentlyContinue)) {
    throw 'Session Manager plugin was not found. Install it or pass -PluginDirectory.'
}

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $LocalPort)
try {
    $listener.Start()
}
catch {
    throw "Local port $LocalPort is already in use; choose another -LocalPort."
}
finally {
    $listener.Stop()
}

Write-Host "Keep this window open and visit http://127.0.0.1:$LocalPort"
Write-Host 'Closing this window ends private access. The EC2 instance must already be running.'
& $AwsExecutable ssm start-session `
    --profile $Profile `
    --region ap-south-1 `
    --no-cli-pager `
    --target $InstanceId `
    --document-name AWS-StartPortForwardingSession `
    --parameters "portNumber=8080,localPortNumber=$LocalPort"
if ($LASTEXITCODE -ne 0) {
    throw "Session Manager exited with status $LASTEXITCODE."
}
