<#
  Watchdog yang dijalankan oleh Scheduled Task "DCC-DisasterCenter".
  Menjalankan server; bila proses berhenti tanpa sengaja (crash, DB putus saat start),
  server dinyalakan lagi setelah 10 detik. Dihentikan dengan stoper_disasterinfo.bat.
#>
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wlog = Join-Path $logDir "watchdog.log"
$clog = Join-Path $logDir "server-console.log"
while ($true) {
  if ((Test-Path $clog) -and (Get-Item $clog).Length -gt 10MB) { Move-Item $clog "$clog.1" -Force }
  Add-Content -Path $wlog -Value ("{0} start server" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
  & $venvPy -m dcc serve 2>&1 | ForEach-Object { "$_" } | Out-File -Append -Encoding utf8 $clog
  Add-Content -Path $wlog -Value ("{0} server berhenti (kode {1}); mulai ulang 10 detik lagi" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $LASTEXITCODE)
  Start-Sleep -Seconds 10
}
