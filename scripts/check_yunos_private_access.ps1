[CmdletBinding()]
param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$LocalStatusUrl = "http://127.0.0.1:$Port/api/status"
$TailscaleCandidates = @(
    "E:\Tailscale\tailscale.exe",
    "$env:ProgramFiles\Tailscale\tailscale.exe",
    "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe"
)

$Listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -First 1
$Status = $null
try {
    $Status = Invoke-RestMethod -Method Get -Uri $LocalStatusUrl -TimeoutSec 3
}
catch {
    $StatusError = $_.Exception.Message
}

$TailscaleService = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
$TailscaleExe = $TailscaleCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
    Select-Object -First 1
if (-not $TailscaleExe) {
    $TailscaleCommand = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($TailscaleCommand) {
        $TailscaleExe = $TailscaleCommand.Source
    }
}

$Healthy = (
    $Status -and
    $Status.bound_host -eq "127.0.0.1" -and
    [int]$Status.bound_port -eq $Port -and
    $Status.readonly -eq $true -and
    $Status.service_mode -eq "real-readonly" -and
    $TailscaleService -and
    $TailscaleService.Status -eq "Running"
)

[pscustomobject]@{
    Healthy = [bool]$Healthy
    Listener = if ($Listener) {
        "$($Listener.LocalAddress):$($Listener.LocalPort)"
    }
    elseif ($Status) {
        "$($Status.bound_host):$($Status.bound_port)"
    }
    else {
        $null
    }
    ListenerProcessId = if ($Listener) {
        $Listener.OwningProcess
    }
    elseif ($Status) {
        $Status.process_id
    }
    else {
        $null
    }
    YunOSStatus = $Status
    YunOSStatusError = $StatusError
    TailscaleService = if ($TailscaleService) { $TailscaleService.Status.ToString() } else { "not-installed" }
    TailscaleExecutable = $TailscaleExe
    ServeStatusCommand = if ($TailscaleExe) { "& `"$TailscaleExe`" serve status" } else { $null }
}

if (-not $Healthy) {
    exit 1
}
