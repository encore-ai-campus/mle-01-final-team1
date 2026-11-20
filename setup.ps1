$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = $PSScriptRoot
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'

function Assert-Command([string] $name, [string] $helpText) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "$name 명령을 찾을 수 없습니다. $helpText"
    }
}

Assert-Command 'uv' '설치: winget install --id astral-sh.uv -e (설치 후 PowerShell 재시작).'
Assert-Command 'node' 'Node.js 22 LTS 이상을 설치하세요.'
Assert-Command 'npm' 'npm은 일반적으로 Node.js 설치에 포함됩니다.'

$nodeVersionText = (& node --version).TrimStart('v').Split('-')[0]
if ($LASTEXITCODE -ne 0) { throw 'Node.js 버전을 확인할 수 없습니다.' }
$nodeVersion = [version]$nodeVersionText
if ($nodeVersion.Major -lt 22) {
    throw "Node.js 22 LTS 이상이 필요합니다. 현재 버전: $nodeVersionText"
}

foreach ($part in @('backend', 'frontend')) {
    $directory = Join-Path $root $part
    $example = Join-Path $directory '.env.example'
    $target = Join-Path $directory '.env'
    if (-not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath $example -Destination $target
        Write-Host "예시 파일을 복사해 $part/.env를 생성했습니다. 배포 전에 설정값을 확인하세요."
    } else {
        Write-Host "기존 $part/.env를 유지합니다."
    }
}

Write-Host '[1/2] uv sync로 백엔드 Python 의존성을 설치합니다...'
Push-Location $backendDir
try {
    & uv sync
    if ($LASTEXITCODE -ne 0) { throw '백엔드 uv sync 설치에 실패했습니다.' }
} finally {
    Pop-Location
}

Write-Host '[2/2] npm으로 프런트엔드 의존성을 설치합니다...'
Push-Location $frontendDir
try {
    if (Test-Path -LiteralPath (Join-Path $frontendDir 'package-lock.json')) {
        & npm ci --no-audit --no-fund
    } else {
        & npm install --no-audit --no-fund
    }
    if ($LASTEXITCODE -ne 0) { throw '프런트엔드 npm 설치에 실패했습니다.' }
} finally {
    Pop-Location
}

Write-Host ''
Write-Host '설치 완료. 실행 명령: powershell -ExecutionPolicy Bypass -File .\start.ps1'
Write-Host '첫 설치 완료 후 backend/uv.lock과 frontend/package-lock.json을 Git에 커밋하세요.'
