param(
    [string]$Url = "http://127.0.0.1:8000/ui/scans",
    [switch]$NoAuth,
    [switch]$Wait,
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

# 1. Read access token from .env (APP_ACCESS_TOKEN) unless -NoAuth is given.
$token = $null
$username = "audit"
if (-not $NoAuth) {
    $envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*APP_ACCESS_TOKEN\s*=\s*(.+?)\s*$') {
                $token = $Matches[1].Trim('"', "'")
            }
            if ($line -match '^\s*APP_USERNAME\s*=\s*(.+?)\s*$') {
                $username = $Matches[1].Trim('"', "'")
            }
        }
    }
    if (-not $token) {
        Write-Warning "APP_ACCESS_TOKEN not found in .env; opening without embedded credentials."
    }
}

# 2. Wait for the API to answer, so the tab does not show a connection error.
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$liveUrl = "http://127.0.0.1:8000/health/live"
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $liveUrl -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # server not up yet
    }
    Start-Sleep -Milliseconds 500
}

if ($ready) {
    Write-Host "API is up: $liveUrl"
} else {
    Write-Warning "API did not answer $liveUrl within $TimeoutSeconds s. Is scripts\start-dev.ps1 running?"
    if ($Wait) { exit 1 }
}

# 3. Build the URL. Credentials in the URL let Chrome send Basic auth
#    on the first request, so no login dialog appears.
$target = $Url
if ($token) {
    $uri = [System.Uri]$Url
    $builder = New-Object System.UriBuilder($uri)
    $builder.UserName = $username
    $builder.Password = $token
    $target = $builder.Uri.AbsoluteUri
}

# 4. Launch a real Chrome window. Resolve the .exe by full path:
#    in git-bash PATH a msys shim named chrome/notepad can shadow the real
#    executable and Start-Process fails or silently forwards to a hidden instance.
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($chrome) {
    Start-Process -FilePath $chrome -ArgumentList "--new-window", $target | Out-Null
    Write-Host "Opened in Chrome: $target"
} else {
    Start-Process $target | Out-Null
    Write-Host "Chrome not found, opened with default browser: $target"
}

Write-Host ""
Write-Host "UI:     $Url"
Write-Host "Login:  $username"
Write-Host "Passwd: $(if ($token) { '<APP_ACCESS_TOKEN from .env>' } else { 'see .env' })"
