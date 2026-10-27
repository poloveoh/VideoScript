param([string]$PythonPath = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'), [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'Stop.ps1') -FunctionsOnly
$env:PYTHONUTF8 = '1'
$portableNode = Join-Path $PSScriptRoot '.tools\node'
if (Test-Path -LiteralPath (Join-Path $portableNode 'node.exe')) {
    $env:PATH = $portableNode + ';' + $env:PATH
}
$address = 'http://127.0.0.1:8501'
$dataPath = Join-Path $PSScriptRoot 'data'
$appPath = Join-Path $PSScriptRoot 'app.py'
function Test-VideoScript {
    try {
        $reply = Invoke-WebRequest -Uri "$address/_stcore/health" -UseBasicParsing -TimeoutSec 2
        return $reply.StatusCode -eq 200
    } catch { return $false }
}
New-Item -ItemType Directory -Force $dataPath | Out-Null
$lock = $null
try {
    $lock = [IO.File]::Open((Join-Path $dataPath 'lifecycle.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $serverPath = Join-Path $dataPath 'server.pid'
    $trayPath = Join-Path $dataPath 'tray.pid'
    $server = Get-ManagedProcess $serverPath 'server'
    if (-not $server) {
        if (Test-VideoScript) { throw 'Port 8501 is already serving an unowned process; nothing was launched or stopped.' }
        if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Local Python environment is missing. See README.md.' }
        # Fail before spawning if policy prevents recording process identity.
        Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop | Out-Null
        $server = Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'streamlit', 'run', "`"$appPath`"", '--server.address', '127.0.0.1', '--server.port', '8501') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath 'server.log') -RedirectStandardError (Join-Path $dataPath 'server-error.log')
        Save-ManagedProcess $server.Id $serverPath 'server'
    }
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (Test-VideoScript) { $ready = $true; break }
        if (-not (Get-ManagedProcess $serverPath 'server')) { throw 'Server stopped. See data/server-error.log.' }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Server startup timed out. See data/server-error.log; Stop.cmd can safely stop it.' }
    if (-not (Get-ManagedProcess $trayPath 'tray')) {
        Remove-Item -LiteralPath (Join-Path $dataPath 'tray.stop') -ErrorAction SilentlyContinue
        $tray = Start-Process -FilePath $PythonPath -ArgumentList @("`"$appPath`"", '--tray-lifecycle') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath 'tray.log') -RedirectStandardError (Join-Path $dataPath 'tray-error.log')
        try { Save-ManagedProcess $tray.Id $trayPath 'tray' }
        catch { Write-Warning "Tray unavailable. Web server remains at $address. See data/tray-error.log. $_" }
    }
} finally {
    if ($lock) { $lock.Dispose() }
}
if (-not $NoBrowser) {
    try { Start-Process $address } catch { Write-Host "VideoScript is running. Open $address in your browser." }
}
