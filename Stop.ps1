param([switch]$FunctionsOnly, [switch]$ServerOnly)
$ErrorActionPreference = 'Stop'

function Get-ManagedProcess($MetadataPath, $Role) {
    if (-not (Test-Path -LiteralPath $MetadataPath)) { return $null }
    try {
        $saved = Get-Content -Raw -LiteralPath $MetadataPath | ConvertFrom-Json
        if ($saved.role -ne $Role -or [int]$saved.pid -le 0) { return $null }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$saved.pid)"
        $created = if ($process.CreationDate -is [datetime]) {
            $process.CreationDate.ToUniversalTime().Ticks.ToString()
        } else { [string]$process.CreationDate }
        if (-not $process -or $process.ProcessId -ne $saved.pid -or
            -not $saved.executable -or $process.ExecutablePath -ne $saved.executable -or
            -not $saved.created -or $created -cne $saved.created -or
            -not $saved.command -or $process.CommandLine -cne $saved.command) { return $null }
        $appPath = Join-Path $PSScriptRoot 'app.py'
        if ([IO.Path]::GetFileName($process.ExecutablePath) -ne 'python.exe') { return $null }
        $appArgument = '(?:^|\s)"?' + [regex]::Escape($appPath) + '"?(?=\s|$)'
        if ($process.CommandLine -notmatch $appArgument) { return $null }
        if ($Role -eq 'server' -and $process.CommandLine -notmatch '-m streamlit run ') { return $null }
        if ($Role -eq 'tray' -and $process.CommandLine -notmatch ' --tray-lifecycle') { return $null }
        return $process
    } catch { return $null }
}

function Save-ManagedProcess($ProcessId, $MetadataPath, $Role) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
    if (-not $process -or -not $process.ExecutablePath -or -not $process.CommandLine) {
        throw 'Cannot validate launched process identity.'
    }
    # Windows venv python.exe is a launcher. Record the real interpreter,
    # otherwise stopping only the launcher would orphan Streamlit.
    $arguments = $process.CommandLine -replace '^\s*(?:"[^"]+"|\S+)\s+', ''
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" |
            Where-Object { $_.ParentProcessId -eq $ProcessId -and
                [IO.Path]::GetFileName($_.ExecutablePath) -eq 'python.exe' -and
                ($_.CommandLine -replace '^\s*(?:"[^"]+"|\S+)\s+', '') -ceq $arguments })
        if ($children.Count -eq 1) { $process = $children[0]; break }
        Start-Sleep -Milliseconds 50
    }
    $created = if ($process.CreationDate -is [datetime]) {
        $process.CreationDate.ToUniversalTime().Ticks.ToString()
    } else { [string]$process.CreationDate }
    @{pid=$process.ProcessId; executable=$process.ExecutablePath;
      created=$created; command=$process.CommandLine; role=$Role} |
        ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $MetadataPath
    if (-not (Get-ManagedProcess $MetadataPath $Role)) { throw 'Launched process identity differs.' }
}

if ($FunctionsOnly) { return }
$dataPath = Join-Path $PSScriptRoot 'data'
$serverPath = Join-Path $dataPath 'server.pid'
$trayPath = Join-Path $dataPath 'tray.pid'

function Stop-Server {
    $server = Get-ManagedProcess $serverPath 'server'
    if ($server) {
        Stop-Process -Id $server.ProcessId
        Wait-Process -Id $server.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    } elseif (Test-Path -LiteralPath $serverPath) {
        Write-Warning 'Server metadata is stale or identity differs; no unrelated process was stopped.'
    }
    Remove-Item -LiteralPath $serverPath -ErrorAction SilentlyContinue
}

if ($ServerOnly) { Stop-Server; return }
$lock = $null
try {
    New-Item -ItemType Directory -Force $dataPath | Out-Null
    $lock = [IO.File]::Open((Join-Path $dataPath 'lifecycle.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $tray = Get-ManagedProcess $trayPath 'tray'
    if ($tray) {
        Set-Content -LiteralPath (Join-Path $dataPath 'tray.stop') -Value ''
        $deadline = (Get-Date).AddSeconds(30)
        while (Get-ManagedProcess $trayPath 'tray') {
            if ((Get-Date) -ge $deadline) { throw 'Tray shutdown timed out; see data/tray-error.log. No process was force-killed.' }
            Start-Sleep -Milliseconds 200
        }
    } elseif (Test-Path -LiteralPath $trayPath) {
        Write-Warning 'Tray metadata is stale or identity differs; no unrelated process was stopped.'
    }
    Remove-Item -LiteralPath $trayPath -ErrorAction SilentlyContinue
    Stop-Server
    Write-Host 'VideoScript stopped. Saved transcripts and videoscript.db remain on disk.'
} finally {
    if ($lock) { $lock.Dispose() }
}
