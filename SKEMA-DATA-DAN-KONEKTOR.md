# Skema Data dan Konektor GRIDCAST

> Catatan 29 Oktober 2026: aplikasi kini bernama **GRIDCAST** (Weather Intelligence & Grid Asset Monitoring, PLN UP2D Sumatera Barat). Isi dokumen ini ditulis saat aplikasi masih bernama Disaster Information Center; istilah teknis seperti database `disaster_center` tetap.

Versi 0.3 · 28 Oktober 2026 · Rancangan teknis, belum migrasi database atau konektor aktif.

Dokumen ini menerjemahkan PRD menjadi kontrak data yang dapat ditinjau pengembang. ArcGIS adalah sumber acuan aset; kejadian, polygon, foto dan histori analisis menjadi data milik aplikasi. PostgreSQL/PostGIS dan object storage adalah usulan target. Pilihan final mengikuti host organisasi, bukan asumsi bahwa Site prototipe saat ini sudah menyediakan keduanya.

## 1 Pemisahan sumber

| Domain | Sumber acuan | Salinan atau hasil di aplikasi |
|---|---|---|
| Aset jaringan | ArcGIS organisasi | Snapshot aset, mapping dan indeks spasial |
| Jumlah pelanggan per gardu | Field ArcGIS atau sumber bisnis yang disetujui | Snapshot agregat dan kelengkapan |
| Peringatan cuaca | BMKG | CAP mentah/normalisasi, revisi dan paparan |
| Cuaca tambahan | Produk vendor berlisensi | Metadata dan data turunan sesuai izin |
| Tagging dan polygon | User/Admin aplikasi | Rekaman, revisi dan audit permanen |
| Foto kejadian | Unggahan User/Admin | Objek file dan metadata di database |
| Gardu nyala/padam | Marco jika nanti dilanjutkan | Saat ini tidak diisi; unknown |

Sinkronisasi ArcGIS tidak menimpa polygon/foto buatan pengguna. Perubahan atau penghapusan aset tetap dapat ditelusuri pada laporan historis. Kode gardu berasal dari identitas bisnis yang diverifikasi, bukan nama marker atau jarak koordinat terdekat.

## 2 Tabel usulan

| Tabel | Field inti | Catatan relasi dan validasi |
|---|---|---|
| users | id, identity_subject, display_name, role, active, created_at | Role admin/user; guest adalah principal anonim, bukan akun dengan password bersama |
| connector_configs | id, provider, service_url, layer_ids, field_mapping, secret_ref, enabled | Rahasia tidak disimpan dalam respons browser; origin/jalur sumber dibatasi |
| sync_runs | id, connector_id, started_at, completed_at, state, source_checkpoint, counts, error | Snapshot hanya published jika tahap wajib selesai |
| asset_versions | id, asset_id, source_key, source_layer, kind, name, feeder_id, geometry, source_edited_at, sync_run_id, active | Satu identitas bisnis stabil dengan versi; referensi geometry/atribut untuk audit |
| feeders | id, source_key, name, gi_id, up3, ulp, snapshot_id | Mapping ke hubungan kelistrikan yang disetujui |
| customer_count_versions | id, asset_id, count, effective_at, source, source_version, quality | count nonnegatif atau null; null berarti belum diketahui |
| incidents | id, title, category, location, occurred_at, description, severity, status, created_by, visibility, revision | Location titik kejadian; tidak diasumsikan titik gardu |
| incident_area_versions | id, incident_id, area_id, name, geometry, revision, created_by, created_at | Polygon/MultiPolygon valid; versi tidak mengubah snapshot lama |
| incident_photos | id, incident_id, tag_location_id, object_key, content_type, bytes, checksum, caption, taken_at, uploaded_by, visibility | Objek dibaca melalui kebijakan akses; thumbnail terpisah |
| weather_records | id, provider, product, kind, source_id, revision, observed_at, issued_at, valid_from, valid_to, fetched_at, geometry_or_grid_ref, quality | Pisahkan observation/forecast/warning; unit/proyeksi tersimpan |
| impact_analyses | id, incident_id, polygon_revision_ids, asset_snapshot_id, customer_snapshot_id, status_snapshot_id, rule_version, computed_at, result | Rekap immutable untuk laporan; status_snapshot_id boleh null |
| impact_analysis_assets | analysis_id, asset_id, relation, known_customer_count, status_at_analysis | Unik per analysis_id dan asset_id; pelanggan tidak diduplikasi |
| alert_reviews | id, alert_id, alert_revision, user_id, reviewed_at, note, state | Peninjauan tidak sama dengan penyelesaian |
| audit_events | id, actor_id, action, entity_type, entity_id, revision, at, before_ref, after_ref | Akun, publikasi guest dan perubahan konfigurasi dapat ditelusuri |
| asset_status_records | asset_id, status, observed_at, fetched_at, source_id, quality | Disiapkan dalam desain saja; konektor Marco ditunda |

Tabel foto boleh dihubungkan langsung ke titik tagging melalui incident/location yang bersangkutan. Jika satu kejadian mempunyai banyak titik lokasi, gunakan entitas lokasi terpisah agar foto tidak ambigu. Penyimpanan metadata pelanggan cukup agregat untuk kebutuhan summary; data pelanggan individual bukan prasyarat rilis awal.

Retensi, soft delete, backup dan ukuran objek ditetapkan sebelum produksi. User dapat mengubah fitur operasional sesuai PRD; audit menyimpan pelaku dan revisi. Untuk edit bersamaan, server membandingkan revision/ETag dan menolak konflik agar perubahan pengguna lain tidak tertimpa diam-diam.

## 3 Konektor ArcGIS

Konfigurasi yang dibutuhkan: base URL FeatureServer, layer ID, autentikasi baca saja, field geometri, kode gardu, kode penyulang, jenis aset, waktu perubahan dan field pelanggan bila ada. Baca metadata sebelum menentukan strategi: `maxRecordCount`, dukungan pagination, ObjectID/GlobalID, spatial reference, edit tracking dan kemampuan perubahan/penghapusan.

Gunakan API Query resmi untuk snapshot awal. Identitas harus stabil lintas sinkronisasi; jika hanya ada ObjectID, verifikasi sifatnya pada sumber dan buat mapping ke kode bisnis. Jika GeoJSON tidak didukung sumber, normalisasikan Esri JSON pada server dengan transformasi proyeksi yang benar. Urutan koordinat internal GeoJSON adalah bujur,lintang.

Full sync ditulis ke staging. Validasi jumlah, kelengkapan halaman, duplikasi dan geometri sebelum publish atomik. Untuk layanan besar, pertimbangkan daftar ID dan batch agar perubahan saat pagination tidak mengakibatkan data terlewat. Hanya snapshot valid yang menggantikan salinan terakhir.

Inkremental memakai edit tracking atau change extraction bila capability dan izin sumber mendukung. Watermark/checkpoint baru disimpan setelah batch sukses. Edit timestamp sendiri tidak cukup untuk mendeteksi delete; lakukan rekonsiliasi ID/full snapshot secara berkala atau gunakan mekanisme delete yang tersedia. Jangan mengaktifkan capability sumber tanpa kewenangan pemilik ArcGIS.

Konektor harus mencatat missing field, asset_id tidak cocok, geometri invalid, token expired dan partial page. Penghapusan hanya diterapkan dari bukti perubahan yang valid atau snapshot lengkap yang berhasil. Data lapangan baru akan terlihat setelah masuk/publish pada ArcGIS lalu tersinkronisasi.

Rujukan: [Query Feature Service](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/) dan [Extract Changes](https://developers.arcgis.com/rest/services-reference/enterprise/extract-changes-feature-service/). Rincian implementasi harus dicocokkan dengan versi dan metadata service yang sebenarnya.

## 4 Kontrak data pelanggan

Jika ArcGIS memiliki field agregat terverifikasi, adapter menghasilkan `asset_id`, `customer_count`, `effective_at`, `source_version` dan `quality`. Jika sumbernya terpisah, gunakan mapping yang sama dan laporan ID yang belum cocok. Bedakan pelanggan terdaftar, aktif dan yang benar-benar padam; tentukan definisi mana yang digunakan sebelum menamai KPI.

Nilai null berbeda dari nol valid. Satu snapshot analisis memakai versi aset dan pelanggan yang diketahui. Bila tanggal sumber berbeda, tampilkan keduanya. Hitungan yang diketahui dapat tetap ditampilkan dengan label cakupan parsial, tidak dipaksakan menjadi total.

Jika pelanggan bisa muncul pada beberapa gardu dalam data mentah, gunakan aturan kepemilikan layanan pada waktu yang sama atau hitung ID pelanggan unik jika tersedia. Tidak menggunakan pembagian kapasitas trafo sebagai pengganti jumlah pelanggan. Estimasi jumlah pelanggan memerlukan metode terpisah dan label yang jelas.

## 5 Kontrak analisis polygon

Input rancangan meliputi ID/revisi polygon tersimpan, mode gabungan area, snapshot aset/pelanggan dan versi aturan. Server memvalidasi geometri dan hak akses lalu memilih gardu pada area penuh. Untuk gardu titik, gunakan semantik covers agar batas ikut; holes tidak ikut. Beberapa polygon digabung atau hasil gardunya dideduplicasi berdasarkan asset_id.

Contoh respons ilustratif berikut bukan data PLN dan bukan endpoint yang sudah berjalan:

```json
{
  "analysis_id": "example-analysis",
  "asset_snapshot_id": "example-assets-v1",
  "customer_snapshot_id": "example-customers-v1",
  "polygon_revision_ids": ["example-area-a-v1", "example-area-b-v1"],
  "gardu_unique_count": 3,
  "known_customer_count": 250,
  "gardu_with_customer_data": 2,
  "gardu_missing_customer_data": 1,
  "customer_total_complete": false,
  "service_state": "not_connected",
  "outage_customer_count": null,
  "scope_label": "Pelanggan pada gardu terpilih; data sebagian",
  "computed_at": "2026-10-28T06:00:00Z"
}
```

Snapshot laporan tidak berubah saat ArcGIS diperbarui. Pilihan “Hitung ulang data terbaru” membuat hasil baru dengan versi terbaru. Ringkasan layar sementara dapat diperbarui saat geometri digeser, tetapi penyimpanan final selalu memperoleh hasil server yang tervalidasi.

## 6 Foto dan publikasi guest

Alur target: User memilih tagging, mengunggah file, server memvalidasi isi/ukuran/tipe, menyimpan objek dan metadata lalu menampilkan thumbnail. Jika unggahan gagal, kejadian tetap tersimpan dengan pesan kegagalan; jangan menyimpan URL seolah foto sudah tersedia. Objek yatim karena kegagalan transaksi dibersihkan melalui pekerjaan terkontrol.

User berhak memakai foto dokumentasi operasional. Guest hanya memperoleh objek yang memang dipublikasikan untuk view. URL akses terbatas bila diperlukan diterbitkan setelah pemeriksaan hak; mengetahui object_key tidak cukup untuk mengakses foto privat. Metadata lokasi dari foto tidak otomatis menimpa koordinat tagging. Pengarsipan foto menghasilkan audit sesuai retensi.

## 7 Cuaca dan batas adapter

Pisahkan adapter visual peta dari adapter data analitis. Windy Map Forecast mengendalikan lapisan peta sesuai API terdokumentasi; mesin summary gardu/pelanggan tetap bekerja dari database sendiri. Point Forecast, jika dipilih, dinormalisasi oleh adapter tersendiri dengan key server serta cache berdasarkan lokasi/model/parameter/waktu.

Setiap lapisan harus memiliki jenis, source/model, unit atau skala AQI, level angin, resolusi, waktu valid, quality dan no-data. Untuk Cb dan petir, model output/indikasi harus dibedakan dari deteksi aktual. Jangan membuat feed analitis dari scraping warna piksel pada tampilan Windy. Hak pemrosesan, redistribusi dan histori mengikuti produk berlisensi yang dipilih.

Tidak ada nilai status gardu yang disimpulkan dari layer cuaca. Kegagalan sumber menandai stale/unavailable dan mempertahankan histori, bukan mengganti nilai dengan nol. Threshold stale berbeda per sumber dan mengikuti interval serta SLA yang disepakati.

## 8 Marco backlog ditunda

Referensi [dashboard Marco](https://up2dsumbar001-pln.github.io/) dicatat berdasarkan pengguna: hijau menyala dan merah padam. Tidak ada penelusuran sumber mesin, scraping, endpoint atau parser yang dijalankan. Format Excel/CSV belum menjadi kontrak aktif setelah koreksi pengguna.

Bila dilanjutkan, adapter status perlu ID gardu stabil, status `on/off/unknown`, waktu observasi, quality dan identitas sumber. Nilai yang terlambat tidak dianggap status terkini. Perubahan warna pada UI saja tidak cukup untuk menyimpulkan kontrak data. Target sistem saat ini mengembalikan status belum tersedia.

## 9 Kontrak API aplikasi yang diusulkan

| Kelompok | Operasi | Hak |
|---|---|---|
| Aset | Query aset dan metadata snapshot | Admin/User; guest hanya proyeksi lapisan guest |
| Kejadian | Buat, ubah, arsipkan dan cari | Write Admin/User; guest view terpilih |
| Polygon | Buat, revisi, simpan dan pilih | Write Admin/User; guest view terpilih |
| Analisis | Jalankan summary dan baca snapshot | Admin/User menjalankan; guest membaca hasil yang dipublikasikan |
| Foto | Unggah, ubah keterangan, arsipkan, lihat | Write Admin/User; read menurut publikasi |
| Akun | Buat, ubah peran, nonaktifkan | Admin |
| Konektor | Konfigurasi, health dan sinkronisasi | Admin; metadata kesehatan yang relevan dapat dibaca User |

Nama route, payload dan strategi deployment final ditentukan saat implementasi. Dokumen ini tidak menambah endpoint yang sudah bisa dipanggil. Rujuk [PRD](./PRD.md) untuk perilaku produk dan [rencana pengembangan](./RENCANA-PENGEMBANGAN.md) untuk urutan pengerjaan.
