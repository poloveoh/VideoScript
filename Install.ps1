param([switch]$SkipModel, [switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    throw 'This installer supports Windows x64. See README.md for manual setup.'
}
$projectRoot = $PSScriptRoot
foreach ($required in @('requirements.lock.txt', 'pipeline.py', 'Start.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $required))) {
        throw "Missing $required. Download the complete repository before running Install.ps1."
    }
}
$toolsRoot = Join-Path $projectRoot '.tools'
$downloads = Join-Path $toolsRoot 'downloads'
New-Item -ItemType Directory -Force $downloads | Out-Null
$env:PYTHONUTF8 = '1'
$env:UV_CACHE_DIR = Join-Path $toolsRoot 'cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $toolsRoot 'python'
$env:UV_PYTHON_NO_REGISTRY = '1'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$existingNode = Get-Command node.exe -ErrorAction SilentlyContinue
$script:downloadNode = $null
if ($existingNode) {
    $fetchType = & $existingNode.Source -p 'typeof fetch' 2>$null
    if ($LASTEXITCODE -eq 0 -and $fetchType -eq 'function') {
        $script:downloadNode = $existingNode.Source
    }
}

function Get-Download([string]$Url, [string]$Destination) {
    Write-Host "Downloading $Url"
    if ($script:downloadNode) {
        # Node is optional here; ordinary Windows PowerShell can download without it.
        & $script:downloadNode -e "fetch(process.argv[1]).then(async r=>{if(!r.ok)throw Error(r.status);require('fs').writeFileSync(process.argv[2],Buffer.from(await r.arrayBuffer()))}).catch(e=>{console.error(e.message);process.exit(1)})" $Url $Destination
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -TimeoutSec 600
    }
}
function Assert-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE). Stop and review the error above." }
}

$uv = Join-Path $toolsRoot 'uv/uv.exe'
if (-not (Test-Path -LiteralPath $uv)) {
    $archive = Join-Path $downloads 'uv.zip'
    Get-Download 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' $archive
    Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $toolsRoot 'uv') -Force
}
if (-not (Test-Path -LiteralPath $uv)) { throw 'uv.exe was not found after extraction.' }

$node = Join-Path $toolsRoot 'node/node.exe'
if (-not (Test-Path -LiteralPath $node)) {
    $indexFile = Join-Path $downloads 'node-index.json'
    Get-Download 'https://nodejs.org/dist/index.json' $indexFile
    $releases = Get-Content -LiteralPath $indexFile -Raw | ConvertFrom-Json
    $release = $releases | Where-Object { $_.lts -ne $false -and $_.files -contains 'win-x64-zip' } | Select-Object -First 1
    if (-not $release -or $release.version -notmatch '^v\d+\.\d+\.\d+$') { throw 'Cannot resolve a Node.js LTS release.' }
    $name = "node-$($release.version)-win-x64"
    $archive = Join-Path $downloads "$name.zip"
    $checksums = Join-Path $downloads 'node-checksums.txt'
    Get-Download "https://nodejs.org/dist/$($release.version)/$name.zip" $archive
    Get-Download "https://nodejs.org/dist/$($release.version)/SHASUMS256.txt" $checksums
    $checksumLine = Get-Content -LiteralPath $checksums | Where-Object { $_ -match ("\s+" + [regex]::Escape("$name.zip") + '$') } | Select-Object -First 1
    if (-not $checksumLine) { throw 'Node.js checksum is missing.' }
    $expected = ($checksumLine -split '\s+')[0]
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw 'Node.js checksum mismatch.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $downloads -Force
    New-Item -ItemType Directory -Force (Join-Path $toolsRoot 'node') | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $downloads $name) | Copy-Item -Destination (Join-Path $toolsRoot 'node') -Recurse -Force
}
$env:PATH = (Join-Path $toolsRoot 'node') + ';' + $env:PATH
& $node --version
Assert-Exit 'Node.js check'
& $uv python install 3.12 --no-bin --no-registry
Assert-Exit 'Python installation'
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & $uv venv (Join-Path $projectRoot '.venv') --python 3.12 --managed-python
    Assert-Exit 'Virtual environment creation'
}
& $uv pip install --python $python -r (Join-Path $projectRoot 'requirements.lock.txt')
Assert-Exit 'Dependency installation'
& $uv pip check --python $python
Assert-Exit 'Dependency check'
Push-Location -LiteralPath $projectRoot
try {
    & $python -c "import streamlit, yt_dlp, faster_whisper, imageio_ffmpeg; print('FFmpeg:', imageio_ffmpeg.get_ffmpeg_exe())"
    Assert-Exit 'Component check'
    if (-not $SkipModel) {
        Write-Host 'Preparing Small multilingual model. This may take several minutes.'
        & $python -c "from pipeline import load_model; load_model('small'); print('Small model ready')"
        Assert-Exit 'Small model download'
    }
    Write-Host 'Installation complete. Start: Start.cmd  Stop: Stop.cmd'
    if (-not $NoLaunch) { & (Join-Path $projectRoot 'Start.ps1') }
} finally {
    Pop-Location
}
