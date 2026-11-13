param(
    [switch]$NoBrowser
)

$root = "C:\work\narrative_audit"
$pg = "C:\work\pgsql"
$data = "C:\work\pgdata"
$log = "C:\work\pg.log"

$running = & "$pg\bin\pg_isready.exe" -h 127.0.0.1 -p 5432 -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting PostgreSQL..."
    Start-Process -FilePath "$pg\bin\pg_ctl.exe" `
        -ArgumentList "-D", $data, "-l", $log, "-o", '"-p 5432"', "start" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 4
}

New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null

$projectProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'uvicorn.exe'")
$apiRunning = @($projectProcesses | Where-Object {
    $_.CommandLine -match [regex]::Escape($root) -and $_.CommandLine -match 'uvicorn.*app\.main:app'
})
$workerRunning = @($projectProcesses | Where-Object {
    $_.CommandLine -match [regex]::Escape($root) -and $_.CommandLine -match '-m\s+app\.worker'
})

if (-not $apiRunning) {
    Write-Host "Starting API on http://127.0.0.1:8000 ..."
    $api = Start-Process -FilePath "$root\.venv\Scripts\uvicorn.exe" `
        -ArgumentList "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload" `
        -WorkingDirectory $root `
        -RedirectStandardOutput "$root\logs\api.out.log" `
        -RedirectStandardError "$root\logs\api.err.log" `
        -WindowStyle Hidden -PassThru
    $api.Id | Out-File -Encoding ascii "$root\logs\api.pid"
}

if (-not $workerRunning) {
    Write-Host "Starting worker..."
    $worker = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
        -ArgumentList "-m", "app.worker" `
        -WorkingDirectory $root `
        -RedirectStandardOutput "$root\logs\worker.out.log" `
        -RedirectStandardError "$root\logs\worker.err.log" `
        -WindowStyle Hidden -PassThru
    $worker.Id | Out-File -Encoding ascii "$root\logs\worker.pid"
}

Start-Sleep -Seconds 4
Write-Host ""
Write-Host "Health:"
& "$root\.venv\Scripts\python.exe" -c @"
import httpx
for path in ('/health/live', '/health/ready'):
    try:
        response = httpx.get('http://127.0.0.1:8000' + path, timeout=5)
        print(f'  {path}: {response.status_code} {response.text}')
    except Exception as exc:
        print(f'  {path}: {type(exc).__name__}')
"@
Write-Host ""
Write-Host "UI:     http://127.0.0.1:8000/ui/scans"
Write-Host "Login:  audit"
Write-Host "Passwd: local-test-token"

if (-not $NoBrowser) {
    Write-Host ""
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\open-ui.ps1"
}
