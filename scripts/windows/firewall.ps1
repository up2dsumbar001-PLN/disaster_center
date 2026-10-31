<#
  Membuka port aplikasi HANYA untuk segmen intranet DCC.
  Jalankan sebagai Administrator:
    powershell -ExecutionPolicy Bypass -File .\scripts\windows\firewall.ps1
#>
param([int]$Port = 18085, [string]$RemoteAddress = "10.17.119.0/24")
$ErrorActionPreference = "Stop"
$name = "DCC Disaster Center TCP $Port"
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
  -RemoteAddress $RemoteAddress -Profile Domain,Private,Public `
  -Description "GRIDCAST: akses web hanya dari $RemoteAddress" | Out-Null
Write-Host "Aturan firewall '$name' dibuat: TCP $Port dari $RemoteAddress." -ForegroundColor Green
Write-Host "Catatan: aplikasi juga menolak IP di luar DCC_ALLOWED_NETWORKS (.env) sebagai lapis kedua."
