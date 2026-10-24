$ErrorActionPreference = 'Stop'
$pidPath = Join-Path $PSScriptRoot 'data/server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Host 'No saved VideoScript server process.'; exit 0 }
$serverId = [int](Get-Content -LiteralPath $pidPath)
$server = Get-Process -Id $serverId -ErrorAction SilentlyContinue
$expected = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($server -and $server.Path -eq $expected) {
    Stop-Process -Id $serverId
    Write-Host 'VideoScript stopped. Saved transcripts remain on disk.'
} elseif ($server) {
    throw 'Process identity differs; no process was stopped.'
}
Remove-Item -LiteralPath $pidPath
