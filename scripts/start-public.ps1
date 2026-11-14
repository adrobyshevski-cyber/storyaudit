param(
    [switch]$OpenBrowser
)

$root = "C:\work\narrative_audit"
$pg = "C:\work\pgsql"
$data = "C:\work\pgdata"
$log = "C:\work\pg.log"
$envFile = Join-Path $root ".env.production"

if (-not (Test-Path $envFile)) {
    Write-Error "Нет $envFile. Скопируйте .env.production.example, заполните секреты и повторите."
    exit 1
}

# All children (alembic, uvicorn, worker) inherit these.
$env:ENV_FILE = $envFile

$running = & "$pg\bin\pg_isready.exe" -h 127.0.0.1 -p 5432 -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting PostgreSQL..."
    Start-Process -FilePath "$pg\bin\pg_ctl.exe" `
        -ArgumentList "-D", $data, "-l", $log, "-o", '"-p 5432"', "start" `
        -WindowStyle Hidden
}

$dbReady = $false
for ($i = 0; $i -lt 30; $i++) {
    & "$pg\bin\pg_isready.exe" -h 127.0.0.1 -p 5432 -q
    if ($LASTEXITCODE -eq 0) { $dbReady = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $dbReady) {
    Write-Error "PostgreSQL did not start. See C:\work\pg.log"
    exit 1
}

New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null

$existing = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'uvicorn.exe'" |
    Where-Object {
        $_.CommandLine -and $_.CommandLine -match [regex]::Escape($root) -and
        $_.CommandLine -match 'uvicorn.*app\.main:app'
    })
if ($existing.Count -gt 0) {
    Write-Host "API уже запущен (PID $($existing.ProcessId -join ', ')). Сначала: powershell -File scripts\stop-dev.ps1"
    exit 1
}

Write-Host "Applying migrations with $envFile ..."
& "$root\.venv\Scripts\python.exe" -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Error "alembic upgrade head failed"
    exit 1
}

Write-Host "Starting API on 127.0.0.1:8000 (no reload) ..."
$api = Start-Process -FilePath "$root\.venv\Scripts\uvicorn.exe" `
    -ArgumentList "app.main:app", "--host", "127.0.0.1", "--port", "8000", `
        "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1" `
    -WorkingDirectory $root `
    -RedirectStandardOutput "$root\logs\api.out.log" `
    -RedirectStandardError "$root\logs\api.err.log" `
    -WindowStyle Hidden -PassThru
$api.Id | Out-File -Encoding ascii "$root\logs\api.pid"

$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health/live" -TimeoutSec 3
        if ($response.status -eq "ok") { $healthy = $true; break }
    } catch { }
}
if (-not $healthy) {
    Write-Error "API did not become healthy. Last errors:"
    Get-Content "$root\logs\api.err.log" -Tail 20 -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "Starting worker ..."
$worker = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "app.worker" `
    -WorkingDirectory $root `
    -RedirectStandardOutput "$root\logs\worker.out.log" `
    -RedirectStandardError "$root\logs\worker.err.log" `
    -WindowStyle Hidden -PassThru
$worker.Id | Out-File -Encoding ascii "$root\logs\worker.pid"

$publicMatch = Select-String -Path $envFile -Pattern '^PUBLIC_BASE_URL=(.+)$' | Select-Object -Last 1
$publicBase = if ($publicMatch) { $publicMatch.Matches.Groups[1].Value.Trim() } else { "" }
Write-Host ""
Write-Host "API:    http://127.0.0.1:8000"
if ($publicBase) { Write-Host "Public: $publicBase" }
Write-Host "Login:  audit (password = APP_ACCESS_TOKEN from .env.production)"
Write-Host "Stop:   powershell -ExecutionPolicy Bypass -File scripts\stop-dev.ps1"

if ($OpenBrowser) {
    Start-Process "http://127.0.0.1:8000/ui/scans"
}

exit 0
