@echo off
setlocal
title YunOS
set "YUNOS_LAUNCH_ROOT=%~dp0"
echo Starting YunOS...
if exist "%~dp0scripts\start_public.ps1" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_public.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $base=Join-Path $env:LOCALAPPDATA 'YunOS'; $app=Join-Path $base 'app-0.2.0'; $mutex=New-Object Threading.Mutex($false,'Local\YunOSPublicDownload'); $held=$false; try { try {$held=$mutex.WaitOne(240000)} catch [Threading.AbandonedMutexException] {$held=$true}; if(-not $held){throw 'Another download is running. Please retry.'}; if(-not (Test-Path -LiteralPath (Join-Path $app 'scripts\start_public.ps1'))) { New-Item -ItemType Directory -Force -Path $base | Out-Null; $id=[guid]::NewGuid().ToString('N'); $zip=Join-Path $base ($id+'.zip'); $stage=Join-Path $base ('download-'+$id); $url='https://github.com/HangJiYun005/YunOS-public/releases/download/v0.2.0'; Write-Host 'Downloading YunOS...'; Invoke-WebRequest -UseBasicParsing -Uri ($url+'/YunOS-0.2.0.zip') -OutFile $zip -TimeoutSec 180; $expected=((Invoke-WebRequest -UseBasicParsing -Uri ($url+'/SHA256SUMS.txt') -TimeoutSec 30).Content -split '\s+')[0]; if($expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected){throw 'Download verification failed'}; Expand-Archive -LiteralPath $zip -DestinationPath $stage; if(Test-Path -LiteralPath $app){throw 'Incomplete app folder exists. Keep data safe and rename app-0.2.0 before retrying.'}; Move-Item -LiteralPath $stage -Destination $app; Remove-Item -LiteralPath $zip } } finally {if($held){$mutex.ReleaseMutex()}; $mutex.Dispose()}; & (Join-Path $app 'scripts\start_public.ps1')"
)
if errorlevel 1 (
  echo.
  echo YunOS could not start. Please check your internet connection and the error above.
  echo Your saved data has not been removed.
  pause
  exit /b 1
)
exit /b 0
