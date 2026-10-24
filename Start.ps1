$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$portableNode = Join-Path $PSScriptRoot '.tools\node'
if (Test-Path -LiteralPath (Join-Path $portableNode 'node.exe')) {
    $env:PATH = $portableNode + ';' + $env:PATH
}
$address = 'http://127.0.0.1:8501'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
function Test-VideoScript {
    try {
        $reply = Invoke-WebRequest -Uri "$address/_stcore/health" -UseBasicParsing -TimeoutSec 2
        return $reply.StatusCode -eq 200
    } catch { return $false }
}
if (-not (Test-VideoScript)) {
    if (-not (Test-Path -LiteralPath $python)) { throw 'Local Python environment is missing. See README.md.' }
    New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot 'data') | Out-Null
    $server = Start-Process -FilePath $python -ArgumentList @('-m', 'streamlit', 'run', 'app.py', '--server.address', '127.0.0.1', '--server.port', '8501') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot 'data/server.log') -RedirectStandardError (Join-Path $PSScriptRoot 'data/server-error.log')
    Set-Content -LiteralPath (Join-Path $PSScriptRoot 'data/server.pid') -Value $server.Id
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (Test-VideoScript) { $ready = $true; break }
        if ($server.HasExited) { throw 'Server stopped. See data/server-error.log.' }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Server startup timed out. See data/server-error.log.' }
}
try { Start-Process $address } catch {
    Write-Host "VideoScript is running. Open $address in your browser."
}
