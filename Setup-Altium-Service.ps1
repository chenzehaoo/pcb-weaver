param(
    [Parameter(Mandatory = $true)][string[]]$ProjectRoot,
    [string]$AltiumExe = '',
    [string]$Python = 'python',
    [string]$Config = 'altium-service.mcp.json'
)
$ErrorActionPreference = 'Stop'

$target = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $PSScriptRoot $Config }
if (Test-Path -LiteralPath $target) {
    throw "Configuration already exists: $target. Choose another -Config path or back up the existing file first."
}
$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    & $Python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project Python environment.' }
}
& $runtime -m pip install 'mcp==1.29.1'
if ($LASTEXITCODE -ne 0) { throw 'Altium MCP dependency installation failed.' }

$arguments = @((Join-Path $PSScriptRoot 'scripts\configure_altium_service.py'))
foreach ($root in $ProjectRoot) { $arguments += @('--project-root', $root) }
if ($AltiumExe) { $arguments += @('--altium-exe', $AltiumExe) }
$arguments += @('--output', $target)
& $runtime @arguments
if ($LASTEXITCODE -ne 0) { throw 'Altium MCP configuration failed.' }
