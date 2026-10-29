# Menjalankan server di jendela ini (untuk uji coba). Tekan Ctrl+C untuk berhenti.
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
& (Join-Path $Root ".venv\Scripts\python.exe") -m dcc serve
