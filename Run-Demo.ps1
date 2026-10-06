param([switch]$Route, [string]$Config = 'toolchain.unified.json')
$ErrorActionPreference = 'Stop'
$runtimePath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'Run Setup.ps1 first.' }
$commandArgs = @('-m', 'pcb_weaver.cli', '--workspace', (Join-Path $PSScriptRoot 'data'), '--config', (Join-Path $PSScriptRoot $Config), 'demo', '--example', (Join-Path $PSScriptRoot 'examples\manufacturing-demo'))
if ($Route) { $commandArgs += '--route' }
& $runtimePath @commandArgs
exit $LASTEXITCODE
