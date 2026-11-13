$root = "C:\work\narrative_audit"
$pg = "C:\work\pgsql"

# uvicorn --reload spawns children through the *base* Python interpreter
# (venv scripts are shims) and those children have no project path in their
# command line. Matching by path therefore leaves orphans that keep port 8000
# busy and keep serving stale code. Match the command line with regexes
# (-like compares the whole string and misses mid-string matches), then kill
# the whole descendant tree.
$pattern = 'uvicorn.*app\.main:app|-m\s+app\.worker'
$self = $PID
$procs = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'uvicorn.exe'")
$livePids = @($procs | ForEach-Object { [int]$_.ProcessId })

$roots = @($procs | Where-Object {
    $_.CommandLine -and $_.CommandLine -match [regex]::Escape($root) -and
    $_.CommandLine -match $pattern
})

$targets = [System.Collections.Generic.HashSet[int]]::new()
$stack = [System.Collections.Generic.Stack[int]]::new()
foreach ($proc in $roots) { [void]$targets.Add([int]$proc.ProcessId); $stack.Push([int]$proc.ProcessId) }
while ($stack.Count -gt 0) {
    $parent = $stack.Pop()
    foreach ($child in $procs | Where-Object { $_.ParentProcessId -eq $parent }) {
        if ($targets.Add([int]$child.ProcessId)) { $stack.Push([int]$child.ProcessId) }
    }
}

$killed = @()
foreach ($procId in $targets) {
    if ($procId -and $procId -ne $self) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        $killed += $procId
    }
}

Start-Sleep -Seconds 2

# PID files written by start-dev.ps1: kill the shim and its descendants even
# when they were reparented (uvicorn --reload children run as spawn_main).
foreach ($file in @("$root\logs\api.pid", "$root\logs\worker.pid")) {
    if (Test-Path $file) {
        $pidValue = (Get-Content $file -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($pidValue -match '^\d+$') {
            $pidInt = [int]$pidValue
            $stack = [System.Collections.Generic.Stack[int]]::new()
            $stack.Push($pidInt)
            while ($stack.Count -gt 0) {
                $parent = $stack.Pop()
                Stop-Process -Id $parent -Force -ErrorAction SilentlyContinue
                $killed += $parent
                foreach ($child in $procs | Where-Object { $_.ParentProcessId -eq $parent }) { $stack.Push([int]$child.ProcessId) }
            }
        }
        Remove-Item $file -Force -ErrorAction SilentlyContinue
    }
}

# Orphaned reload children: spawn_main with a parent that no longer exists.
foreach ($proc in $procs) {
    if ($proc.CommandLine -match 'multiprocessing[\.]spawn') {
        if ($livePids -notcontains [int]$proc.ParentProcessId) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            $killed += [int]$proc.ProcessId
        }
    }
}

Start-Sleep -Seconds 1

# Last resort: whatever still listens on 8000 belongs to this dev stack.
$listeners = @()
try {
    $listeners = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop).OwningProcess
} catch {
    $listeners = netstat -ano | Select-String ":8000\s+\S+\s+LISTENING\s+(\d+)" |
        ForEach-Object { [int]$_.Matches[0].Groups[1].Value }
}
foreach ($proc in ($listeners | Sort-Object -Unique)) {
    if ($proc -and $proc -ne $self) {
        Stop-Process -Id $proc -Force -ErrorAction SilentlyContinue
        $killed += $proc
    }
}

& "$pg\bin\pg_ctl.exe" -D "C:\work\pgdata" stop -m fast

if ($killed) {
    Write-Host "Stopped processes: $(($killed | Sort-Object -Unique) -join ', ')"
} else {
    Write-Host "No API/worker processes were running."
}
Write-Host "API, worker and PostgreSQL stopped."
