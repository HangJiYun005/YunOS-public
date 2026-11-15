[CmdletBinding()]
param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$LocalStatusUrl = "http://127.0.0.1:$Port/api/status"
try {
    $Status = Invoke-RestMethod -Method Get -Uri $LocalStatusUrl -TimeoutSec 3
}
catch {
    $Status = $null
}

if (-not $Status) {
    $PortLine = netstat.exe -ano -p tcp |
        Select-String -Pattern "^\s*TCP\s+\S+:${Port}\s+\S+\s+LISTENING\s+\d+\s*$" |
        Select-Object -First 1
    if ($PortLine) {
        throw "Refusing to stop port ${Port}: it is listening but did not return YunOS status."
    }
    [pscustomobject]@{
        Result = "already-stopped"
        Port = $Port
        TailscaleConnectionChanged = $false
        ServeConfigurationChanged = $false
    }
    exit 0
}

if ($Status.bound_host -ne "127.0.0.1" -or
    [int]$Status.bound_port -ne $Port -or
    $Status.readonly -ne $true -or
    $Status.service_mode -ne "real-readonly") {
    throw "Refusing to stop PID $($Status.process_id): it is not the expected private read-only YunOS runtime."
}

$RuntimeProcessId = [int]$Status.process_id
$PortOwner = netstat.exe -ano -p tcp |
    Select-String -Pattern "^\s*TCP\s+127\.0\.0\.1:${Port}\s+\S+\s+LISTENING\s+${RuntimeProcessId}\s*$" |
    Select-Object -First 1
if (-not $PortOwner) {
    throw "Refusing to stop PID ${RuntimeProcessId}: listener ownership check failed."
}

$Process = Get-Process -Id $RuntimeProcessId -ErrorAction SilentlyContinue
if (-not $Process -or $Process.ProcessName -ne "python") {
    throw "Refusing to stop PID ${RuntimeProcessId}: process ownership check failed."
}

Stop-Process -Id $RuntimeProcessId -Force
$Deadline = (Get-Date).AddSeconds(10)
do {
    Start-Sleep -Milliseconds 250
    $Remaining = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
} until (-not $Remaining -or (Get-Date) -ge $Deadline)

if ($Remaining) {
    throw "YunOS process stopped, but port $Port is still listening."
}

[pscustomobject]@{
    Result = "stopped"
    ProcessId = $RuntimeProcessId
    Port = $Port
    TailscaleConnectionChanged = $false
    ServeConfigurationChanged = $false
}
