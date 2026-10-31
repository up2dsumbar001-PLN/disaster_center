<#
  Memasang GRIDCAST agar berjalan otomatis saat Windows menyala.

  - Bila nssm.exe tersedia (PATH atau folder tools\), dipasang sebagai Windows Service "DCC-DisasterCenter".
  - Bila tidak, dibuat Scheduled Task "DCC-DisasterCenter" (akun SYSTEM, saat startup, restart bila berhenti).

  Jalankan sebagai Administrator:
    powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-service.ps1
  Hapus: tambahkan -Uninstall
#>
param([switch]$Uninstall, [string]$Name = "DCC-DisasterCenter")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "Jalankan setup.ps1 terlebih dahulu (.venv belum ada)." }
$nssm = (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source
if (-not $nssm -and (Test-Path (Join-Path $Root "tools\nssm.exe"))) { $nssm = Join-Path $Root "tools\nssm.exe" }

if ($Uninstall) {
  & (Join-Path $PSScriptRoot "dcc-control.ps1") -Action stop -NoPause
  if ($nssm -and (Get-Service $Name -ErrorAction SilentlyContinue)) { & $nssm stop $Name; & $nssm remove $Name confirm }
  if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $Name; Unregister-ScheduledTask -TaskName $Name -Confirm:$false }
  Write-Host "Layanan $Name dihapus."
  return
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\logs") | Out-Null
if ($nssm) {
  if (Get-Service $Name -ErrorAction SilentlyContinue) { & $nssm stop $Name | Out-Null; & $nssm remove $Name confirm | Out-Null }
  & $nssm install $Name $venvPy "-m dcc serve"
  & $nssm set $Name AppDirectory $Root
  & $nssm set $Name DisplayName "GRIDCAST (DCC UP2D Sumbar)"
  & $nssm set $Name Description "GRIDCAST - Weather Intelligence & Grid Asset Monitoring, PLN UP2D Sumatera Barat (port 18085)"
  & $nssm set $Name Start SERVICE_AUTO_START
  & $nssm set $Name AppStdout (Join-Path $Root "data\logs\service.out.log")
  & $nssm set $Name AppStderr (Join-Path $Root "data\logs\service.err.log")
  & $nssm set $Name AppRotateFiles 1
  & $nssm set $Name AppRotateBytes 10485760
  & $nssm set $Name AppExit Default Restart
  & $nssm set $Name AppRestartDelay 5000
  & $nssm start $Name
  Write-Host "Windows Service '$Name' terpasang dan berjalan (NSSM)." -ForegroundColor Green
} else {
  $loop = Join-Path $Root "scripts\windows\service-loop.ps1"
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$loop`"" -WorkingDirectory $Root
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $trigger.Delay = "PT30S"   # beri waktu PostgreSQL dan jaringan menyala dulu
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    & (Join-Path $PSScriptRoot "dcc-control.ps1") -Action stop -NoPause
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
  }
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "GRIDCAST PLN UP2D Sumatera Barat (port 18085)" | Out-Null
  Start-ScheduledTask -TaskName $Name
  Write-Host "Scheduled Task '$Name' dibuat (startup, akun SYSTEM) dan dijalankan." -ForegroundColor Green
  Write-Host "Log aplikasi: data\logs\dcc.log"
}
Start-Sleep -Seconds 4
try {
  $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18085/api/health" -TimeoutSec 10
  Write-Host "Cek kesehatan: $($r.StatusCode) $($r.Content.Substring(0, [Math]::Min(120, $r.Content.Length)))"
} catch { Write-Warning "Server belum menjawab. Periksa data\logs\dcc.log" }
