param(
    [string]$OutDir = "C:\work\narrative_audit\backups",
    [int]$KeepDays = 14,
    [string]$Database = "storyaudit",
    [string]$DbUser = "storyaudit",
    [string]$DbHost = "127.0.0.1"
)

$pg = "C:\work\pgsql"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$file = Join-Path $OutDir "$Database-$stamp.dump"

Write-Host "Dumping $Database to $file ..."
& "$pg\bin\pg_dump.exe" -h $DbHost -U $DbUser -Fc -f $file $Database
if ($LASTEXITCODE -ne 0) {
    Write-Error "pg_dump failed"
    exit 1
}

# A dump that pg_restore cannot even list is not a backup.
& "$pg\bin\pg_restore.exe" -l $file | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "pg_restore cannot read the new dump; keeping it for inspection"
    exit 1
}

Get-ChildItem -Path $OutDir -Filter "$Database-*.dump" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Force

$size = [math]::Round((Get-Item $file).Length / 1MB, 1)
Write-Host "OK: $file ($size MB), kept last $KeepDays days"
