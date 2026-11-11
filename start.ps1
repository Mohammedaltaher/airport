param(
    [ValidateSet('dev', 'built')][string]$Mode = 'dev',
    [string]$PythonPath = ''
)
$ErrorActionPreference = 'Stop'
$dmaRoot = $PSScriptRoot
if (-not $PythonPath) {
    $candidates = @(
        (Join-Path $dmaRoot '.venv/Scripts/python.exe'),
        (Join-Path $dmaRoot '../labs/day1_lab3/.runtime/python.exe')
    )
    $PythonPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$backendPath = Join-Path $dmaRoot 'backend'
$frontendPath = Join-Path $dmaRoot 'frontend'
$env:PYTHONUTF8 = '1'
& $PythonPath -c 'import fastapi, pandas, google.genai, uvicorn'
if ($LASTEXITCODE -ne 0) { throw 'Install backend/requirements.txt with the selected Python first.' }
if ($Mode -eq 'built') {
    if (-not (Test-Path -LiteralPath (Join-Path $frontendPath 'dist/index.html'))) { throw 'Run npm run build inside dma/frontend first.' }
    Write-Host 'DMA: http://127.0.0.1:8000 | Ctrl+C to stop'
    & $PythonPath -m uvicorn app.main:app --app-dir $backendPath --host 127.0.0.1 --port 8000
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendPath 'node_modules'))) { throw 'Run npm ci inside dma/frontend first.' }
$dmaNpm = (Get-Command npm -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path (Join-Path $dmaRoot 'logs') | Out-Null
$dmaBackend = Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', "`"$backendPath`"", '--host', '127.0.0.1', '--port', '8000') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dmaRoot 'logs/server-output.log') -RedirectStandardError (Join-Path $dmaRoot 'logs/server-error.log')
try {
    Start-Sleep -Seconds 2
    if ($dmaBackend.HasExited) { throw 'Backend failed to start. Check dma/logs/server-error.log; port 8000 may already be occupied.' }
    Write-Host 'DMA: http://127.0.0.1:4321 | API: http://127.0.0.1:8000/docs | Ctrl+C to stop'
    Push-Location $frontendPath
    try { & $dmaNpm run dev } finally { Pop-Location }
} finally {
    if (-not $dmaBackend.HasExited) { Stop-Process -Id $dmaBackend.Id }
}
