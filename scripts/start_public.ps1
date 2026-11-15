[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'YunOS'),
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeVersion = '3.14.10'
# SHA-256 values published at https://www.python.org/downloads/release/python-31410/
$RuntimeHashes = @{
    'amd64' = '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
    'arm64' = 'bbe116bb1248820359cbf25127ac5c7794ea161869d83686cfbc9a45203ff836'
    'win32' = '11a28e8c00a6d8f56b0a6164d52546133a9ed015a5b33e04536917e4fd6e306c'
}
if ([Environment]::OSVersion.Version.Major -lt 10) { throw 'YunOS requires Windows 10 or later.' }
$MachineArch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$RuntimeArch = switch ($MachineArch.ToUpperInvariant()) { 'AMD64' { 'amd64' }; 'ARM64' { 'arm64' }; 'X86' { 'win32' }; default { throw "Unsupported architecture: $MachineArch" } }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
$SetupMutex = New-Object Threading.Mutex($false, 'Local\YunOSPublicRuntimeSetup')
$HasMutex = $false
try {
    try { $HasMutex = $SetupMutex.WaitOne(180000) } catch [Threading.AbandonedMutexException] { $HasMutex = $true }
    if (-not $HasMutex) { throw 'Another YunOS setup is running. Please retry shortly.' }
    $RuntimeDir = Join-Path $InstallRoot "runtime-$RuntimeVersion-$RuntimeArch"
    $PythonExe = Join-Path $RuntimeDir 'python.exe'
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeDir 'ready.txt'))) {
        Write-Host 'Preparing YunOS runtime. The first launch needs an internet connection...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $DownloadPath = Join-Path $InstallRoot ('python-' + [guid]::NewGuid().ToString('N') + '.zip')
        $StageDir = Join-Path $InstallRoot ('runtime-stage-' + [guid]::NewGuid().ToString('N'))
        $RuntimeUrl = "https://www.python.org/ftp/python/$RuntimeVersion/python-$RuntimeVersion-embed-$RuntimeArch.zip"
        Invoke-WebRequest -UseBasicParsing -Uri $RuntimeUrl -OutFile $DownloadPath -TimeoutSec 180
        $HashStream = [IO.File]::OpenRead($DownloadPath)
        $Hasher = [Security.Cryptography.SHA256]::Create()
        try { $ActualHash = [BitConverter]::ToString($Hasher.ComputeHash($HashStream)).Replace('-', '').ToLowerInvariant() }
        finally { $HashStream.Dispose(); $Hasher.Dispose() }
        if ($ActualHash -ne $RuntimeHashes[$RuntimeArch]) {
            throw 'Runtime checksum mismatch. Nothing was installed. Please retry.'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($DownloadPath, $StageDir)
        & (Join-Path $StageDir 'python.exe') -I -c 'import sqlite3, json, http.server, hashlib; print(sqlite3.sqlite_version)'
        if ($LASTEXITCODE -ne 0) { throw 'Runtime cannot start on this computer.' }
        if (Test-Path -LiteralPath $RuntimeDir) {
            # Preserve incomplete installations, never recursively delete a computed path.
            $ResolvedRuntime = [IO.Path]::GetFullPath($RuntimeDir)
            if (-not $ResolvedRuntime.StartsWith($InstallRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid runtime path' }
            Move-Item -LiteralPath $RuntimeDir -Destination ($RuntimeDir + '-incomplete-' + [guid]::NewGuid().ToString('N'))
        }
        Move-Item -LiteralPath $StageDir -Destination $RuntimeDir
        [IO.File]::WriteAllText((Join-Path $RuntimeDir 'ready.txt'), $RuntimeHashes[$RuntimeArch])
        Remove-Item -LiteralPath $DownloadPath
    }
} finally {
    if ($HasMutex) { $SetupMutex.ReleaseMutex() }
    $SetupMutex.Dispose()
}
$ServerPath = Join-Path $ProjectRoot 'public_app\server.py'
if (-not (Test-Path -LiteralPath $ServerPath)) { throw 'Application files are missing. Download YunOS again.' }
$DataDir = Join-Path $InstallRoot 'data'
$LogDir = Join-Path $InstallRoot 'logs'
New-Item -ItemType Directory -Path $DataDir, $LogDir -Force | Out-Null
$LaunchId = [guid]::NewGuid().ToString('N')
$Arguments = '-I "' + $ServerPath + '" --data-dir "' + $DataDir + '"'
if ($NoBrowser) { $Arguments += ' --no-browser' }
$Child = Start-Process -FilePath $PythonExe -ArgumentList $Arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir "$LaunchId.out.log") -RedirectStandardError (Join-Path $LogDir "$LaunchId.err.log")
$StatePath = Join-Path $DataDir 'service.json'
$Ready = $false
for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    try {
        $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $ServiceUri = [uri]$State.url
        if ($ServiceUri.Scheme -ne 'http' -or $ServiceUri.Host -ne '127.0.0.1') { throw 'Invalid service address' }
        $Status = Invoke-RestMethod -Uri ($State.url + '/api/session') -TimeoutSec 2
        if ($Status.token -eq $State.token) { $Ready = $true; break }
    } catch { }
    $Child.Refresh()
    if ($Child.HasExited -and $Child.ExitCode -ne 0) { break }
    Start-Sleep -Milliseconds 250
}
if (-not $Ready) { throw "YunOS did not start. See logs in $LogDir" }
Write-Host ('YunOS is ready: ' + $State.url)
Write-Host ('Your library: ' + $DataDir)
