# Status implementasi terhadap PRD v0.3

Versi aplikasi 1.1 · 29 Oktober 2026 (tambah adapter Tomorrow.io). Status "Ada" artinya fitur sudah terpasang dan lolos pengujian otomatis dengan data sintetis serta impor KML asli (14.173 aset). Pengujian di PC DCC dengan BMKG langsung dan pengguna nyata (UAT) masih diperlukan.

| ID | Kebutuhan | Status | Catatan |
|---|---|---|---|
| DIC01 | ArcGIS sebagai master aset | Sebagian | Snapshot aset, upsert idempoten, penonaktifan aset hilang, riwayat snapshot. Sumber saat ini KML/KMZ ekspor ArcGIS/Google Earth. Konektor REST ArcGIS menunggu URL FeatureServer dan izin baca. |
| DIC02 | Identitas gardu & sumber pelanggan | Ada | Kunci `globalid_1` per trafo, `LOCATION` sebagai ID gardu. Unggah pelanggan Excel/CSV dengan pencocokan LOCATION, kode gardu, ASSETNUM atau nama, laporan baris tidak cocok, dan versi set. |
| DIC03 | BMKG & kesehatan sumber | Ada | Collector server tiap 60 detik. Update/Cancel/Test/expired ditangani. Kegagalan tampil sebagai "status belum diketahui", bukan aman. |
| DIC04 | Admin/User/Guest | Ada | Sesi di database, scrypt, wajib ganti sandi awal, header anti-CSRF, guest hanya baca lapisan yang dipilih Admin. |
| DIC05 | Titik tagging permanen | Ada | Kategori, status, dampak, PIC, catatan, visibilitas, revisi, dan penolakan edit bersamaan (409). |
| DIC06 | Polygon permanen | Ada | Gambar/edit titik di peta, validasi self-intersection, revisi tersimpan. |
| DIC07 | Rekap gardu/pelanggan | Ada | Semantik covers (batas ikut, lubang tidak), gabungan area tanpa duplikasi, null ≠ 0, contoh PRD lulus. Snapshot tidak berubah walau data pelanggan diperbarui. |
| DIC08 | Foto pada kejadian | Ada | Validasi isi gambar di server, thumbnail, EXIF waktu, visibilitas publik/internal, tersimpan di disk. |
| DIC09 | Audit & histori | Ada | Audit login, akun, impor, kejadian, area, foto, rekap, dan pengaturan. |
| DIC10 | Windy | Sebagian | Tautan Windy. Integrasi Map Forecast menunggu key/lisensi. |
| DIC11 | Awan Cb & angin | Sebagian | Panah angin 10 m + CAPE (Open-Meteo, opsional) berlabel "bukan deteksi Cb". Tambahan Tomorrow.io: angin, hembusan, dan cuaca per kantor UP3 (opsional). |
| DIC12 | AQI | Sebagian | US AQI + PM2.5/PM10 model CAMS (Open-Meteo, opsional), berlabel bukan ISPU. AQI Tomorrow.io tidak termasuk paket gratis. |
| DIC13 | Potensi & kejadian petir | Belum | Menunggu feed petir berlisensi (BMKG/Xweather/Tomorrow.io berbayar). |
| DIC14 | Radar/nowcast & ETA | Sebagian | Radar RainViewer 2 jam terakhir (skema warna 2 sesuai aturan 2026). Tomorrow.io: tile intensitas hujan saat ini dan perkiraan jam mulai hujan/hujan lebat 6 jam per UP3 (model, bukan ETA sel badai). |
| DIC15 | Alarm berkelanjutan & eskalasi | Sebagian | Collector berjalan tanpa browser, antrean WA dengan deduplikasi dan retry. Eskalasi bertingkat belum ada. |
| DIC16 | Peta Google | Belum | Basemap dapat diganti lewat pengaturan (OSM, Esri, atau tile internal). |
| DIC17 | Marco | Ditunda | Status gardu ditampilkan "belum tersedia". |
| DIC18 | SCADA | Ditunda | Di luar lingkup. |

Catatan lisensi: API gratis Open-Meteo hanya untuk non-komersial. Untuk operasional PLN, aktifkan dengan API key berbayar. RainViewer (gratis hanya untuk pribadi/edukasi), Tomorrow.io (paket gratis 25 panggilan/jam, 500/hari) dan Esri basemap juga memiliki ketentuan pemakaian masing-masing.

Semua lapisan cuaca tambahan (Open-Meteo, RainViewer, Tomorrow.io) default nonaktif dan diaktifkan Admin di **Administrasi, Konektor dan tampilan**. Peringatan resmi tetap hanya dari BMKG.
