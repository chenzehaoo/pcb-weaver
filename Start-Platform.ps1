param([int]$Port = 8765, [string]$Config = 'toolchain.unified.json')
$ErrorActionPreference = 'Stop'
$runtimePath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'Run Setup.ps1 first.' }
Write-Host "PCB Weaver: http://127.0.0.1:$Port"
& $runtimePath -m pcb_weaver.platform --workspace (Join-Path $PSScriptRoot 'data') --config (Join-Path $PSScriptRoot $Config) --port $Port
exit $LASTEXITCODE
