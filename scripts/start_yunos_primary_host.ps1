[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ConfigPath = Join-Path $ProjectRoot "config_private\primary_host.json"
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Primary host configuration is missing on this computer."
}
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Config.schema_version -ne 1 -or
    $Config.role -ne "primary" -or
    $Config.host_name -ne $env:COMPUTERNAME -or
    $Config.project_root -ne $ProjectRoot -or
    $Config.web_host -ne "127.0.0.1" -or
    $Config.service_mode -ne "real-readonly" -or
    $Config.database_sync_enabled -ne $false -or
    $Config.provider_enabled -ne $false) {
    throw "This installation does not match the configured primary host."
}
$Port = [int]$Config.web_port
if ($Port -lt 1024 -or $Port -gt 65535) {
    throw "Invalid primary host port."
}
foreach ($RelativePath in @(
    "data\ayunos_chatgpt.db",
    "data\profile\approved_profile.db",
    "data\semantic\ayunos_semantic.db",
    "data\runtime\ayunos_runtime.db",
    "data\operational\ayunos_operational_state.db"
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $RelativePath) -PathType Leaf)) {
        throw "A required primary host database is missing: $RelativePath"
    }
}
& (Join-Path $ProjectRoot "scripts\start_yunos_private_access.ps1") -Port $Port
