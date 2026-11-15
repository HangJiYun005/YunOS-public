[CmdletBinding()]
param(
    [string]$ValueName = "YunOS Private Readonly Access",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$StartScript = Join-Path $ProjectRoot "scripts\start_yunos_private_access.ps1"
$PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $StartScript -PathType Leaf)) {
    throw "Startup script not found: $StartScript"
}

$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$ExpectedCommand = (
    "`"$PowerShellExe`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass " +
    "-File `"$StartScript`" -Port $Port"
)
$Existing = Get-ItemProperty -Path $RunKey -Name $ValueName -ErrorAction SilentlyContinue
if ($Existing) {
    $ExistingCommand = $Existing.$ValueName
    if ($ExistingCommand -ne $ExpectedCommand) {
        throw "Startup entry '$ValueName' already exists with a different command; refusing to overwrite it."
    }
    [pscustomobject]@{
        Result = "already-registered"
        Mechanism = "HKCU Run"
        ValueName = $ValueName
        Command = $ExistingCommand
    }
    exit 0
}

New-Item -Path $RunKey -Force | Out-Null
New-ItemProperty `
    -Path $RunKey `
    -Name $ValueName `
    -PropertyType String `
    -Value $ExpectedCommand `
    -Force |
    Out-Null

[pscustomobject]@{
    Result = "registered"
    Mechanism = "HKCU Run"
    ValueName = $ValueName
    RunLevel = "Limited"
    DuplicateProcessPolicy = "start script reuses only the verified 127.0.0.1:$Port real-readonly listener"
    Command = $ExpectedCommand
}
