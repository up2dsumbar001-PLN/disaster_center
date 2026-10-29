# Disaster Information Center: DCC UP2D Sumatera Barat

Web app untuk dispatcher DCC. Peta jaringan distribusi 20 kV disatukan dengan peringatan resmi BMKG, pencatatan kejadian bencana, polygon dampak, foto dokumentasi, dan rekap gardu serta pelanggan di dalam area. Aplikasi ini **mendukung keputusan** operator. Aplikasi tidak menjalankan perintah SCADA atau switching.

Spesifikasi produk: [PRD.md](./PRD.md) v0.3. Status setiap butir PRD: [STATUS-IMPLEMENTASI.md](./STATUS-IMPLEMENTASI.md).

## Arsitektur (tanpa Docker)

```
Browser intranet 10.17.119.0/24
        │  http://<IP-PC>:18085
        ▼
Python 3.10+  ── Starlette + uvicorn (1 proses)
  ├─ API + frontend statis (Leaflet, tanpa CDN, tanpa build)
  ├─ Collector BMKG (CAP tiap 60 dtk, gempa tiap 5 mnt) ─► internet bmkg.go.id
  ├─ Antrean notifikasi ─► WA gateway (Node.js, HTTP)
  └─ Foto ─► folder data\photos
        │
        ▼
PostgreSQL (instance yang sudah ada, bersama event downloader)
  ├─ database event_downloader   (TimescaleDB, milik aplikasi lain)
  ├─ database disaster_center    ◄── user dcc_app (aplikasi ini)
  └─ database wa_gateway         (opsional, bila gateway butuh DB)
```

Satu instance PostgreSQL dipakai bersama, tetapi **setiap aplikasi memiliki database dan user sendiri**. Backup cukup dikelola di satu tempat, sementara gangguan atau migrasi pada satu aplikasi tidak merusak data aplikasi lain. Web app dan WA gateway berhubungan lewat HTTP, tidak saling membaca tabel. PostGIS **tidak diperlukan**: analisis spasial dihitung di Python (modul `dcc/geo.py`) dan geometri disimpan sebagai GeoJSON.

## Instalasi di PC DCC (Windows)

Prasyarat: Python 3.10+, PostgreSQL yang sudah berjalan, dan akses internet dari PC ke `bmkg.go.id` serta server tile peta.

```powershell
cd C:\Users\DCC-KANAN\Documents\disaster_center
# 1. venv + paket + database + .env + firewall (jalankan PowerShell sebagai Administrator)
powershell -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
# 2. uji coba di jendela ini
.\scripts\windows\run.ps1
# 3. setelah lancar, pasang agar otomatis menyala saat Windows start
powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-service.ps1
```

- Login pertama memakai `admin` dengan kata sandi acak yang tertulis di `data\ADMIN-AWAL.txt`. Kata sandi itu wajib diganti saat login pertama, lalu berkas tersebut dihapus.
- Impor aset: buka **Administrasi**, pilih **Data aset**, lalu klik **Impor** pada `ASET JARINGAN SUMBAR.kml`. Prosesnya sekitar 30 detik dan menghasilkan 14.173 aset. Impor juga bisa lewat CLI: `.venv\Scripts\python -m dcc import-kml "ASET JARINGAN SUMBAR.kml"`.
- Data pelanggan: buka **Administrasi**, pilih **Data pelanggan**, unduh template (berisi seluruh gardu), isi kolom `JUMLAH_PELANGGAN`, lalu unggah kembali.
- Akses dari jaringan: `http://<IP-PC-ini>:18085`. Ada dua lapis pembatas, yaitu aturan Windows Firewall (hanya 10.17.119.0/24) dan `DCC_ALLOWED_NETWORKS` di `.env`.
- Backup harian: `scripts\windows\backup.ps1` membackup database (pg_dump) dan foto. Keduanya diperlukan untuk pemulihan.

## Menyalakan, menghentikan, memantau

Klik dua kali berkas berikut di folder aplikasi. Windows akan meminta izin Administrator untuk menyalakan atau menghentikan, karena server berjalan dengan akun SYSTEM.

| Berkas | Fungsi |
|---|---|
| `start_disasterinfo.bat` | Menyalakan server, lalu menunggu sampai siap |
| `stoper_disasterinfo.bat` | Menghentikan server sekarang. Autostart saat Windows menyala tetap aktif. |
| `restart_disasterinfo.bat` | Menghentikan lalu menyalakan lagi, misalnya setelah memperbarui kode atau `.env` |
| `viewer_disasterinfo.bat` | Panel pemantau: status server, autostart, alamat akses, kesehatan sumber BMKG/WA, log terakhir, dan menu nyalakan/hentikan/restart/buka browser/log langsung |

Autostart: Scheduled Task `DCC-DisasterCenter` berjalan 30 detik setelah Windows menyala, sebelum ada pengguna login. Task ini menjalankan watchdog `scripts\windows\service-loop.ps1` yang menyalakan ulang server bila proses berhenti tanpa sengaja. Saat start, server menunggu hingga 10 menit bila PostgreSQL belum siap. Log tersimpan di `data\logs\` (`dcc.log`, `watchdog.log`).

Perintah lain: `python -m dcc check` (uji database), `python -m dcc reset-password <user>`.

## Peran pengguna

| | Admin | User | Tamu (tanpa login) |
|---|---|---|---|
| Lihat peta, peringatan, kejadian publik | ✓ | ✓ | ✓ (lapisan yang diizinkan Admin) |
| Catat/ubah kejadian, polygon, foto, rekap | ✓ | ✓ | – |
| Akun, impor aset/pelanggan, konektor, WA, audit | ✓ | – | – |

## Integrasi WA gateway

Buka **Administrasi**, lalu **Notifikasi WA**. Isi URL endpoint kirim pesan pada gateway Node.js, header token, dan templat body, misalnya `{"number":"{{target}}","message":"{{message}}"}`. Setelah itu isi nomor atau ID grup tujuan. Server mengirim:
- peringatan BMKG baru yang mengenai aset, berisi jumlah gardu, penyulang, dan masa berlaku;
- kejadian baru atau perubahan status dengan dampak minimal yang dipilih.

Pesan masuk antrean di database dan dikirim ulang otomatis (backoff) bila gateway mati. Tombol **Kirim pesan uji** memeriksa format API gateway.

## Struktur kode

| Path | Isi |
|---|---|
| `dcc/app.py` | Rute API, otorisasi, audit |
| `dcc/collector.py` | Collector BMKG, paparan aset, adapter Open-Meteo/RainViewer, antrean WA |
| `dcc/tomorrow.py` | Adapter Tomorrow.io: prakiraan 6 jam per UP3, tile hujan, penjaga kuota gratis |
| `dcc/cap.py` | Parser CAP BMKG (Update/Cancel/Test/expired) |
| `dcc/geo.py` | Geometri: covers, lubang polygon, koridor, validasi polygon |
| `dcc/analysis.py` | Rekap gardu/pelanggan per area (PRD §7) |
| `dcc/kml_import.py`, `dcc/assets.py` | Impor KML/KMZ, snapshot aset, indeks spasial |
| `dcc/customers.py` | Unggah jumlah pelanggan Excel/CSV |
| `dcc/schema.sql` | Skema database (migrasi otomatis saat start) |
| `static/` | Frontend (index.html, app.js, app.css, Leaflet lokal) |
| `tests/` | 28 pengujian (model + API end-to-end ke PostgreSQL) |
| `scripts/windows/` | setup, run, install-service, firewall, backup |
| `_arsip_draft_nextjs/` | Draft lama (Next.js/Cloudflare), disimpan untuk referensi |

Pengujian: `pip install pytest` lalu `python -m pytest tests` (butuh PostgreSQL lokal; atur `DCC_TEST_PG`).
