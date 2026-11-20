param(
    [ValidateSet('both', 'backend', 'frontend')]
    [string] $Mode = 'both'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = $PSScriptRoot
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'

if ($Mode -eq 'backend') {
    Set-Location -LiteralPath $backendDir
    & uv run --no-sync uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
    exit $LASTEXITCODE
}

if ($Mode -eq 'frontend') {
    Set-Location -LiteralPath $frontendDir
    & npm run dev -- --host 127.0.0.1
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath (Join-Path $backendDir '.venv'))) {
    throw 'backend/.venv를 찾을 수 없습니다. 먼저 setup.ps1을 실행하세요.'
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'node_modules'))) {
    throw 'frontend/node_modules를 찾을 수 없습니다. 먼저 setup.ps1을 실행하세요.'
}

Write-Host '백엔드와 프런트엔드를 각각 새 PowerShell 창에서 실행합니다...'
Start-Process -FilePath 'powershell.exe' -ArgumentList "-NoExit -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Mode backend"
Start-Process -FilePath 'powershell.exe' -ArgumentList "-NoExit -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Mode frontend"
Write-Host 'Frontend: http://127.0.0.1:5173'
Write-Host 'Backend:  http://127.0.0.1:8000/api/health'
Write-Host '개발 서버를 종료하려면 새로 열린 터미널 창을 닫으세요.'
