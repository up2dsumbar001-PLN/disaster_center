<#
  Disaster Information Center - instalasi di Windows (tanpa Docker)

  Yang dilakukan:
    1. Membuat virtualenv Python .venv dan memasang paket dari requirements.txt
    2. Membuat role "dcc_app" dan database "disaster_center" pada PostgreSQL yang sudah ada
       (instance yang sama dengan event downloader, database dan user TERPISAH)
    3. Menulis berkas .env (bila belum ada) dan menguji koneksi + membuat tabel
    4. Opsional: membuat aturan Windows Firewall port 18085 hanya untuk 10.17.119.0/24

  Jalankan dari PowerShell (Run as Administrator bila ingin sekalian membuat aturan firewall):
    powershell -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
#>
param(
  [string]$PgHost = "127.0.0.1",
  [int]$PgPort = 5432,
  [string]$PgSuperUser = "postgres",
  [string]$DbName = "disaster_center",
  [string]$DbUser = "dcc_app",
  [int]$AppPort = 18085,
  [string]$AllowedNetwork = "10.17.119.0/24",
  [switch]$SkipDb,
  [switch]$SkipFirewall
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

# ------------------------------------------------------------------ Python
Step "Memeriksa Python"
$pyExe = $null; $pyArgs = @()
foreach ($cand in @(@("py", "-3"), @("python"), @("python3"))) {
  try {
    $a = @(); if ($cand.Length -gt 1) { $a = $cand[1..($cand.Length - 1)] }
    $v = & $cand[0] @a -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v) { $pyExe = $cand[0]; $pyArgs = $a; $ver = "$v".Trim(); break }
  } catch { }
}
if (-not $pyExe) { throw "Python tidak ditemukan. Pasang Python 3.10+ dari python.org (centang 'Add to PATH')." }
Write-Host "Python $ver ($pyExe $pyArgs)"
if ([version]$ver -lt [version]"3.10") { throw "Butuh Python 3.10 atau lebih baru (terpasang $ver)." }

Step "Membuat virtualenv .venv dan memasang paket"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $pyExe @pyArgs -m venv .venv }
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install gagal. Bila memakai proxy kantor, set dulu: `$env:HTTPS_PROXY='http://proxy:port'" }

# ---------------------------------------------------------------- Database
$dbPassword = $null
if (-not $SkipDb) {
  Step "Mendeteksi layanan PostgreSQL"
  $svcs = @(Get-CimInstance Win32_Service -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "postgres|pgsql|timescale" -or $_.PathName -match "pg_ctl" })
  foreach ($s in $svcs) {
    $port = $null
    if ($s.PathName -match '-D\s+"?([^"]+?)"?(\s+-|$)') {
      $conf = Join-Path $Matches[1].Trim() "postgresql.conf"
      if (Test-Path $conf) {
        $line = Select-String -Path $conf -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
      }
    }
    if (-not $port) { $port = 5432 }
    Write-Host ("  {0} [{1}] port {2}" -f $s.Name, $s.State, $port)
    if (-not $PSBoundParameters.ContainsKey("PgPort") -and $s.State -eq "Running" -and $svcs.Count -eq 1) { $PgPort = $port }
  }
  if (-not $svcs.Count) { Write-Warning "Layanan PostgreSQL tidak terdeteksi; memakai $PgHost`:$PgPort." }
  if ($svcs.Count -gt 1 -and -not $PSBoundParameters.ContainsKey("PgPort")) {
    Write-Warning "Lebih dari satu layanan PostgreSQL. Memakai port $PgPort; jalankan ulang dengan -PgPort <port> bila salah."
  }

  Step "Membuat role $DbUser dan database $DbName di $PgHost`:$PgPort"
  $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
  $dbPassword = -join (1..24 | ForEach-Object { $chars | Get-Random })
  $env:DCC_INIT_APP_PASSWORD = $dbPassword
  & $venvPy -m dcc init-db --host $PgHost --port $PgPort --superuser $PgSuperUser --dbname $DbName --user $DbUser
  $code = $LASTEXITCODE
  Remove-Item Env:\DCC_INIT_APP_PASSWORD
  if ($code -ne 0) { throw "Pembuatan database gagal (lihat pesan di atas)." }
}

# --------------------------------------------------------------------- .env
Step "Menulis konfigurasi .env"
$envFile = Join-Path $Root ".env"
$dsn = "host=$PgHost port=$PgPort dbname=$DbName user=$DbUser password=$dbPassword"
if (Test-Path $envFile) {
  $content = Get-Content $envFile -Raw
  if ($dbPassword) {
    $content = [regex]::Replace($content, "(?m)^DCC_DATABASE_URL=.*$", "DCC_DATABASE_URL=$dsn")
    Set-Content -Path $envFile -Value $content -Encoding UTF8
    Write-Host ".env sudah ada; DCC_DATABASE_URL diperbarui."
  } else { Write-Host ".env sudah ada; tidak diubah." }
} else {
  $content = Get-Content (Join-Path $Root ".env.example") -Raw
  $content = $content -replace "(?m)^DCC_PORT=.*$", "DCC_PORT=$AppPort"
  $content = $content -replace "(?m)^DCC_ALLOWED_NETWORKS=.*$", "DCC_ALLOWED_NETWORKS=$AllowedNetwork,127.0.0.1/32,::1/128"
  if ($dbPassword) { $content = [regex]::Replace($content, "(?m)^DCC_DATABASE_URL=.*$", "DCC_DATABASE_URL=$dsn") }
  $secret = -join ((1..32) | ForEach-Object { "{0:x2}" -f (Get-Random -Maximum 256) })
  $content = $content -replace "(?m)^DCC_SECRET_KEY=.*$", "DCC_SECRET_KEY=$secret"
  Set-Content -Path $envFile -Value $content -Encoding UTF8
  Write-Host ".env dibuat."
}
# Batasi akses berkas .env (berisi kata sandi DB) ke Administrators, SYSTEM dan pengguna saat ini.
try {
  & icacls $envFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" "$($env:USERNAME):F" | Out-Null
} catch { Write-Warning "Tidak dapat mengatur izin .env: $_" }

Step "Menguji koneksi database dan membuat tabel"
& $venvPy -m dcc check
if ($LASTEXITCODE -ne 0) { throw "Uji koneksi gagal. Periksa DCC_DATABASE_URL di .env dan pg_hba.conf." }

# ---------------------------------------------------------------- Firewall
if (-not $SkipFirewall) {
  Step "Aturan Windows Firewall"
  $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($isAdmin) {
    & (Join-Path $PSScriptRoot "firewall.ps1") -Port $AppPort -RemoteAddress $AllowedNetwork
  } else {
    Write-Warning "Bukan Administrator: lewati firewall. Jalankan nanti sebagai Administrator: .\scripts\windows\firewall.ps1"
  }
}

Write-Host ""
Write-Host "Selesai." -ForegroundColor Green
Write-Host "  Jalankan uji coba : .\scripts\windows\run.ps1"
Write-Host "  Pasang sebagai layanan otomatis: .\scripts\windows\install-service.ps1 (Administrator)"
Write-Host "  Impor aset       : Administrasi > Data aset > 'ASET JARINGAN SUMBAR.kml' > Impor"
Write-Host "  Akses            : http://<IP-PC-ini>:$AppPort  dari jaringan $AllowedNetwork"
Write-Host "  Kata sandi admin awal tertulis di data\ADMIN-AWAL.txt setelah server pertama kali dijalankan."
