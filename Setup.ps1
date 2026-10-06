param([string]$Python = 'python', [string]$Config = 'toolchain.unified.json')
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimePath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePath)) {
    & $Python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project Python environment.' }
}
& $runtimePath -m pip install -r (Join-Path $projectRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
& $runtimePath -m pip install -e "${projectRoot}[dev]"
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $runtimePath (Join-Path $projectRoot 'scripts\configure_client.py') --config $Config
if ($LASTEXITCODE -ne 0) { throw 'MCP configuration failed.' }
& $runtimePath -m pcb_weaver.cli --workspace (Join-Path $projectRoot 'data') --config (Join-Path $projectRoot $Config) doctor
