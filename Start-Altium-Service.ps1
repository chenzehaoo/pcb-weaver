param([switch]$Once, [int]$PollSeconds = 1, [string]$Config = 'altium-service.mcp.json')
$ErrorActionPreference = 'Stop'

$path = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $PSScriptRoot $Config }
if (-not (Test-Path -LiteralPath $path)) { throw "MCP configuration not found: $path. Run Setup-Altium-Service.ps1 first." }
$settings = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
$server = $settings.mcpServers.'altium-local-developer-beta'
if (-not $server -or -not $server.command -or -not $server.env) { throw 'Invalid Altium service MCP configuration.' }
$python = [string]$server.command
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python environment not found: $python" }
foreach ($item in $server.env.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($item.Name, [string]$item.Value, 'Process')
}
if ($env:ALTIUM_SERVICE_NATIVE_LAUNCH -ne '0') {
    throw 'This launcher requires ALTIUM_SERVICE_NATIVE_LAUNCH=0 for the developer beta.'
}

$arguments = @((Join-Path $PSScriptRoot 'scripts\altium_service_worker.py'), '--poll-seconds', $PollSeconds)
if ($Once) { $arguments += '--once' }
& $python @arguments
exit $LASTEXITCODE
