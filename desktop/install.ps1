[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $ProjectRoot 'output\desktop-build\YunOS-win32-x64'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\YunOS'
if (-not (Test-Path -LiteralPath (Join-Path $Source 'YunOS.exe'))) {
    throw 'Build YunOS Desktop before installing.'
}
$Running = Get-Process -Name YunOS -ErrorAction SilentlyContinue
if ($Running) { throw 'Close YunOS before updating its desktop application.' }
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Get-ChildItem -LiteralPath $Source | Copy-Item -Destination $InstallRoot -Recurse -Force
$Exe = Join-Path $InstallRoot 'YunOS.exe'
$Programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Shell = New-Object -ComObject WScript.Shell
foreach ($Entry in @(
    @{ Path = (Join-Path $Programs 'YunOS.lnk'); Args = ''; Description = 'YunOS 本地记忆工作区' },
    @{ Path = (Join-Path $Programs 'YunOS 启动器.lnk'); Args = '--launcher'; Description = 'YunOS 主机状态与启动器' },
    @{ Path = (Join-Path $Desktop 'YunOS.lnk'); Args = ''; Description = 'YunOS 本地记忆工作区' }
)) {
    $Shortcut = $Shell.CreateShortcut($Entry.Path)
    $Shortcut.TargetPath = $Exe
    $Shortcut.Arguments = $Entry.Args
    $Shortcut.WorkingDirectory = $InstallRoot
    $Shortcut.IconLocation = "$Exe,0"
    $Shortcut.Description = $Entry.Description
    $Shortcut.Save()
}
Write-Output "Installed: $Exe"
Write-Output 'Start menu: YunOS / YunOS 启动器'
