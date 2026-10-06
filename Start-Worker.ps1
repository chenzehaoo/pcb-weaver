param([string]$Config = 'toolchain.unified.json')
$ErrorActionPreference = 'Stop'
$runtimePath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'Run Setup.ps1 first.' }
Write-Host 'PCB Weaver worker: keep this process running for queued MCP jobs.'
& $runtimePath -m pcb_weaver.worker --workspace (Join-Path $PSScriptRoot 'data') --config (Join-Path $PSScriptRoot $Config)
exit $LASTEXITCODE
