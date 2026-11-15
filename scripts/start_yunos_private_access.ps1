[CmdletBinding()]
param(
    [int]$Port = 8765,
    [int]$StartupTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RunScript = Join-Path $ProjectRoot "run.ps1"
$PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$LocalStatusUrl = "http://127.0.0.1:$Port/api/status"

function Get-YunOSStatus {
    try {
        return Invoke-RestMethod -Method Get -Uri $LocalStatusUrl -TimeoutSec 3
    }
    catch {
        return $null
    }
}

function Assert-PrivateReadonlyStatus {
    param([Parameter(Mandatory = $true)]$Status)

    if ($Status.bound_host -ne "127.0.0.1" -or
        [int]$Status.bound_port -ne $Port -or
        $Status.readonly -ne $true -or
        $Status.service_mode -ne "real-readonly") {
        throw "Port $Port is not serving the expected private read-only YunOS runtime."
    }
}

$ExistingStatus = Get-YunOSStatus
if ($ExistingStatus) {
    Assert-PrivateReadonlyStatus -Status $ExistingStatus
    [pscustomobject]@{
        Result = "already-running"
        ProcessId = [int]$ExistingStatus.process_id
        BoundAddress = "127.0.0.1:$Port"
        Readonly = $true
    }
    exit 0
}

$ExistingListener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($ExistingListener) {
    throw "Port $Port is already in use, but it did not return YunOS status."
}

if (-not (Test-Path -LiteralPath $RunScript -PathType Leaf)) {
    throw "YunOS launcher not found: $RunScript"
}

$Arguments = (
    "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass " +
    "-File `"$RunScript`" web-real-readonly --port $Port"
)
$Shell = New-Object -ComObject Shell.Application
$Shell.ShellExecute($PowerShellExe, $Arguments, $ProjectRoot, "open", 0)

$Deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
do {
    Start-Sleep -Milliseconds 300
    $Status = Get-YunOSStatus
} until ($Status -or (Get-Date) -ge $Deadline)

if (-not $Status) {
    throw "YunOS did not become ready on 127.0.0.1:$Port."
}

Assert-PrivateReadonlyStatus -Status $Status
[pscustomobject]@{
    Result = "started"
    ProcessId = [int]$Status.process_id
    BoundAddress = "127.0.0.1:$Port"
    Readonly = $true
}
