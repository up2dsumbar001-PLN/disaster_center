<#
  Backup database + foto kejadian (keduanya diperlukan untuk pemulihan).
  Hasil: backups\dcc-<waktu>.dump (pg_dump custom format) dan backups\foto-<waktu>.zip
  Simpan otomatis lewat Task Scheduler harian, mis.:
    schtasks /Create /SC DAILY /ST 01:30 /RU SYSTEM /TN DCC-Backup /TR "powershell -ExecutionPolicy Bypass -File <path>\scripts\windows\backup.ps1"
  Pemulihan: pg_restore -h 127.0.0.1 -U postgres -d disaster_center --clean backups\dcc-....dump
             lalu ekstrak foto-....zip ke folder data\
#>
param([int]$Keep = 14)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$envFile = Join-Path $Root ".env"
$dsn = (Get-Content $envFile | Where-Object { $_ -match "^DCC_DATABASE_URL=" }) -replace "^DCC_DATABASE_URL=", ""
$kv = @{}
foreach ($p in $dsn.Split(" ")) { if ($p -match "=") { $k, $v = $p.Split("=", 2); $kv[$k] = $v } }
$pgdump = (Get-Command pg_dump.exe -ErrorAction SilentlyContinue).Source
if (-not $pgdump) {
  # Cari folder bin dari layanan PostgreSQL yang terpasang (lokasi instalasi bisa di mana saja).
  foreach ($svc in Get-CimInstance Win32_Service | Where-Object { $_.PathName -match "pg_ctl" }) {
    $exe = ($svc.PathName -replace '^"?([^"]*pg_ctl(\.exe)?)"?.*$', '$1')
    $cand = Join-Path (Split-Path $exe) "pg_dump.exe"
    if (Test-Path $cand) { $pgdump = $cand; break }
  }
}
if (-not $pgdump) { $pgdump = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_dump.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName }
if (-not $pgdump) { throw "pg_dump.exe tidak ditemukan. Tambahkan folder bin PostgreSQL ke PATH." }
$out = Join-Path $Root "backups"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$env:PGPASSWORD = $kv["password"]
& $pgdump -h $kv["host"] -p $kv["port"] -U $kv["user"] -Fc -f (Join-Path $out "dcc-$stamp.dump") $kv["dbname"]
if ($LASTEXITCODE -ne 0) { throw "pg_dump gagal" }
Remove-Item Env:\PGPASSWORD
$photos = Join-Path $Root "data\photos"
if (Test-Path $photos) { Compress-Archive -Path $photos -DestinationPath (Join-Path $out "foto-$stamp.zip") -Force }
Get-ChildItem $out -File | Sort-Object LastWriteTime -Descending | Select-Object -Skip ($Keep * 2) | Remove-Item
Write-Host "Backup selesai: $out\dcc-$stamp.dump"
