<#
  Panel kendali Disaster Information Center.
    -Action start    menyalakan server (lewat Scheduled Task bila terpasang)
    -Action stop     menghentikan server sekarang (autostart saat Windows menyala TETAP aktif)
    -Action restart  hentikan lalu nyalakan lagi
    -Action status   ringkasan status satu kali
    -Action viewer   tampilan pemantau dengan menu (default)
  Dipanggil oleh start_disasterinfo.bat, stoper_disasterinfo.bat, viewer_disasterinfo.bat di folder aplikasi.
#>
param(
  [ValidateSet("start", "stop", "restart", "status", "viewer")]
  [string]$Action = "viewer",
  [switch]$NoPause
)
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$TaskName = "DCC-DisasterCenter"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Port = 18085
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
  $line = Get-Content $envFile | Where-Object { $_ -match "^\s*DCC_PORT\s*=\s*(\d+)" } | Select-Object -First 1
  if ($line -and $line -match "(\d+)\s*$") { $Port = [int]$Matches[1] }
}
$LogFile = Join-Path $Root "data\logs\dcc.log"

function Test-Admin {
  ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Elevated([string]$act) {
  # Menjalankan aksi start/stop/restart sebagai Administrator (diperlukan untuk proses milik SYSTEM).
  $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action $act"
  try {
    $p = Start-Process powershell.exe -Verb RunAs -ArgumentList $arg -Wait -PassThru
    return $p.ExitCode
  } catch {
    Write-Host "Dibatalkan: izin Administrator diperlukan." -ForegroundColor Yellow
    return 1
  }
}

function Get-Task { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

function Get-ServerProcs {
  $ids = @()
  try { $ids += @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess) } catch { }
  $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
      ($_.CommandLine -match "-m dcc serve") -or ($_.CommandLine -match "service-loop\.ps1") })
  $ids += @($procs | Select-Object -ExpandProperty ProcessId)
  return @($ids | Where-Object { $_ -and $_ -ne $PID } | Sort-Object -Unique)
}

function Get-Health {
  try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 4 } catch { return $null }
}

function Wait-Health([int]$seconds) {
  for ($i = 0; $i -lt $seconds; $i++) {
    $h = Get-Health
    if ($h) { return $h }
    Start-Sleep -Seconds 1
  }
  return $null
}

function Start-Server {
  if (Get-Health) { Write-Host "Server sudah berjalan di port $Port." -ForegroundColor Green; return 0 }
  if (-not (Test-Path $venvPy)) { Write-Host "Belum terpasang. Jalankan scripts\windows\setup.ps1." -ForegroundColor Red; return 1 }
  $task = Get-Task
  if ($task) {
    if (-not (Test-Admin)) { return (Invoke-Elevated "start") }
    Write-Host "Menyalakan Scheduled Task $TaskName ..."
    Start-ScheduledTask -TaskName $TaskName | Out-Null
  } else {
    Write-Host "Scheduled Task belum dipasang; menyalakan server di latar belakang (tidak otomatis saat Windows menyala)."
    $loop = Join-Path $Root "scripts\windows\service-loop.ps1"
    Start-Process powershell.exe -WindowStyle Hidden -WorkingDirectory $Root `
      -ArgumentList "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$loop`""
  }
  Write-Host "Menunggu server siap (maks. 90 detik; bisa lebih lama bila database belum siap)..."
  $h = Wait-Health 90
  if ($h) { Write-Host "Server berjalan: http://127.0.0.1:$Port" -ForegroundColor Green; return 0 }
  Write-Host "Server belum menjawab. Periksa log: $LogFile" -ForegroundColor Yellow
  return 1
}

function Stop-Server {
  $task = Get-Task
  $procs = Get-ServerProcs
  if (-not $procs.Count -and -not ($task -and $task.State -eq "Running")) {
    Write-Host "Server memang tidak berjalan."
    return 0
  }
  if (-not (Test-Admin)) { return (Invoke-Elevated "stop") }
  if ($task -and $task.State -eq "Running") {
    Write-Host "Menghentikan Scheduled Task $TaskName ..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
  }
  # Hentikan watchdog dulu agar tidak menyalakan ulang, lalu proses server.
  for ($round = 0; $round -lt 3; $round++) {
    $loops = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "service-loop\.ps1" -and $_.ProcessId -ne $PID })
    foreach ($p in $loops) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach ($id in (Get-ServerProcs)) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    if (-not (Get-ServerProcs).Count) { break }
  }
  if ((Get-ServerProcs).Count) { Write-Host "Sebagian proses masih berjalan: $((Get-ServerProcs) -join ', ')" -ForegroundColor Red; return 1 }
  Write-Host "Server dihentikan. Autostart saat Windows menyala tetap aktif." -ForegroundColor Green
  return 0
}

function Show-Status([switch]$Compact) {
  $task = Get-Task
  $h = Get-Health
  $procs = Get-ServerProcs
  Write-Host ""
  if ($h) {
    $up = [TimeSpan]::FromSeconds([double]$h.uptime_s)
    Write-Host ("  SERVER    : BERJALAN  (uptime {0}h {1}m, aset dimuat {2})" -f [Math]::Floor($up.TotalHours), $up.Minutes, $h.assets_loaded) -ForegroundColor Green
  } elseif ($procs.Count) {
    Write-Host "  SERVER    : PROSES ADA, BELUM MENJAWAB (sedang start atau menunggu database)" -ForegroundColor Yellow
  } else {
    Write-Host "  SERVER    : BERHENTI" -ForegroundColor Red
  }
  if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    $auto = if ($task.State -eq "Disabled") { "NONAKTIF" } else { "AKTIF (saat Windows menyala, akun SYSTEM)" }
    Write-Host "  AUTOSTART : $auto"
    if ($info) { Write-Host ("  TASK      : {0}, terakhir dijalankan {1}" -f $task.State, $info.LastRunTime) }
  } else {
    Write-Host "  AUTOSTART : BELUM DIPASANG (jalankan scripts\windows\install-service.ps1 sebagai Administrator)" -ForegroundColor Yellow
  }
  Write-Host ("  PROSES    : {0}" -f $(if ($procs.Count) { "PID " + ($procs -join ", ") } else { "-" }))
  $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -ExpandProperty IPAddress)
  Write-Host ("  ALAMAT    : http://127.0.0.1:{0}" -f $Port)
  foreach ($ip in $ips) { Write-Host ("              http://{0}:{1}" -f $ip, $Port) }
  if ($h -and $h.sources) {
    Write-Host "  SUMBER DATA:"
    foreach ($s in $h.sources) {
      $color = switch ($s.state) { "ok" { "Green" } "partial" { "Yellow" } default { "Red" } }
      $when = ""
      if ($s.checked_at) { try { $when = ([DateTime]$s.checked_at).ToLocalTime().ToString("HH:mm:ss") } catch { } }
      $err = ""
      if ($s.error) { $err = " - " + ("$($s.error)").Substring(0, [Math]::Min(70, ("$($s.error)").Length)) }
      Write-Host ("    {0,-12} {1,-8} dicek {2}{3}" -f $s.source, $s.state, $when, $err) -ForegroundColor $color
    }
  }
  if (-not $Compact -and (Test-Path $LogFile)) {
    Write-Host ""
    Write-Host "  LOG TERAKHIR ($LogFile):" -ForegroundColor Cyan
    Get-Content $LogFile -Tail 12 -Encoding UTF8 | ForEach-Object { "    " + $_.Substring(0, [Math]::Min(150, $_.Length)) }
  }
}

function Follow-Log {
  if (-not (Test-Path $LogFile)) { Write-Host "Belum ada log."; Start-Sleep 2; return }
  Clear-Host
  Write-Host "LOG LANGSUNG - tekan tombol apa saja untuk kembali" -ForegroundColor Cyan
  Get-Content $LogFile -Tail 40 -Encoding UTF8
  $pos = (Get-Item $LogFile).Length
  while (-not [Console]::KeyAvailable) {
    Start-Sleep -Milliseconds 800
    $len = (Get-Item $LogFile).Length
    if ($len -lt $pos) { $pos = 0 }
    if ($len -gt $pos) {
      $fs = [IO.File]::Open($LogFile, "Open", "Read", "ReadWrite")
      try {
        $fs.Seek($pos, "Begin") | Out-Null
        $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
        Write-Host -NoNewline $sr.ReadToEnd()
        $pos = $fs.Position
      } finally { $fs.Dispose() }
    }
  }
  [void][Console]::ReadKey($true)
}

function Show-Viewer {
  $host.UI.RawUI.WindowTitle = "DCC Disaster Information Center - Viewer"
  while ($true) {
    Clear-Host
    Write-Host "=====================================================================" -ForegroundColor Cyan
    Write-Host "  DISASTER INFORMATION CENTER  -  DCC UP2D Sumatera Barat" -ForegroundColor Cyan
    Write-Host ("  {0}" -f (Get-Date -Format "dddd, dd MMMM yyyy HH:mm:ss")) -ForegroundColor Cyan
    Write-Host "=====================================================================" -ForegroundColor Cyan
    Show-Status
    Write-Host ""
    Write-Host "  [1] Nyalakan  [2] Hentikan  [3] Restart  [4] Buka di browser"
    Write-Host "  [5] Log langsung  [6] Buka folder log  [0] Keluar   (refresh otomatis 10 detik)"
    $deadline = (Get-Date).AddSeconds(10)
    $key = $null
    while ((Get-Date) -lt $deadline) {
      if ([Console]::KeyAvailable) { $key = [Console]::ReadKey($true).KeyChar; break }
      Start-Sleep -Milliseconds 200
    }
    switch ($key) {
      "1" { Start-Server | Out-Null; Start-Sleep 2 }
      "2" { Stop-Server | Out-Null; Start-Sleep 2 }
      "3" { if (Test-Admin) { Stop-Server | Out-Null; Start-Server | Out-Null } else { Invoke-Elevated "restart" | Out-Null }; Start-Sleep 2 }
      "4" { Start-Process "http://127.0.0.1:$Port/" }
      "5" { Follow-Log }
      "6" { Start-Process explorer.exe (Join-Path $Root "data\logs") }
      "0" { return }
      "q" { return }
    }
  }
}

$code = 0
switch ($Action) {
  "start" { $code = Start-Server }
  "stop" { $code = Stop-Server }
  "restart" { if (Test-Admin) { Stop-Server | Out-Null; $code = Start-Server } else { $code = Invoke-Elevated "restart"; $NoPause = $true } }
  "status" { Show-Status }
  "viewer" { Show-Viewer; $NoPause = $true }
}
if (-not $NoPause -and $Action -ne "status") { Show-Status -Compact; Write-Host ""; Read-Host "Tekan Enter untuk menutup" | Out-Null }
exit $code
