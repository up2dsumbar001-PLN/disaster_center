# Web App Disaster Information Center — Sumatera Barat

Prototipe dashboard pemantauan BMKG dan paparan aset distribusi 20 kV. Root proyek sekarang langsung berada di `C:\Users\DCC-KANAN\Documents\disaster_center`; jalankan perintah dari folder tersebut.

Dokumen proyek:

- [PRD versi 0.3](./PRD.md): ArcGIS, cuaca, tagging, polygon, rekap pelanggan, foto dan Admin/User/Guest.
- [Rencana pengembangan](./RENCANA-PENGEMBANGAN.md): tahapan, dependensi dan backlog.
- [Biaya peta dan layanan cuaca](./BIAYA-DAN-LAYANAN.md): dampak izin Node.js, OSM, harga Google dan rekomendasi vendor.
- [Skema data dan konektor](./SKEMA-DATA-DAN-KONEKTOR.md): rancangan database, sinkronisasi dan kontrak summary.
- [Rancangan awal DCC](./RANCANGAN-DCC.md): konteks prototipe awal; keputusan terbaru mengikuti PRD versi 0.3.

Konektor Marco ditunda sesuai instruksi pengguna. Hijau menyala dan merah padam dicatat sebagai referensi; status aktual belum diintegrasikan. Dokumen versi 0.3 adalah spesifikasi pengembangan, bukan klaim seluruh fitur baru sudah aktif pada prototipe.

Izin firewall Node.js tidak diubah. Server lokal tidak dijalankan pada pekerjaan relokasi dan dokumentasi ini. Build lama dalam `dist` merupakan artefak yang dipindahkan; buat build baru dari root sebelum menjalankan hasil produksi lokal berikutnya.

## Menjalankan

Node.js >=22.13.0 dan npm diperlukan.

```powershell
npm run install:ci
npm run dev -- --host 127.0.0.1
```

Buka alamat Local yang dicetak. Untuk lingkungan Codex pada komputer ini, npm dapat dijalankan melalui runtime pnpm yang tersedia; tidak diperlukan pemasangan global.

```powershell
& 'C:\Users\DCC-KANAN\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd' --package=npm dlx -c 'npm run dev -- --host 127.0.0.1'
```

## Penggunaan

1. Ekspor proyek Google Earth menjadi KML/KMZ, lalu pilih **Impor aset jaringan**. GeoJSON FeatureCollection WGS84 juga didukung. Batas prototipe: 20 MB unggahan, 40 MB hasil ekstraksi KML, 30.000 fitur. NetworkLink harus diekspor menjadi geometri langsung.
2. Aset diproses di memori sesi browser dan tidak diunggah ke server. Impor mengganti baseline sesi sebelumnya. Data hilang ketika halaman di-reload. Aset permanen dan pengelolaan versi merupakan tahap produksi.
3. Feed BMKG diperiksa setiap 60 detik. Area CAP aktif untuk Sumatera Barat dibandingkan dengan titik aset dan koridor ruas. Pengaturan koridor tersedia melalui ikon pengaturan.
4. Pilih **Coba simulasi** untuk data latihan yang diberi label jelas. Simulasi tidak mengubah baseline impor asli. Prakiraan aktual dinonaktifkan pada simulasi; panel kesehatan sumber dan kejadian gempa tetap berlabel BMKG/nasional.
5. Aktifkan suara secara manual jika diperlukan. Bunyi hanya muncul untuk paparan baru pada halaman aktif. **Ditinjau** dan ekspor laporan hanya berlaku untuk sesi; belum ada notifikasi ke luar aplikasi atau audit terpusat.
6. Prakiraan dimuat manual menggunakan kode ADM4 Kemendagri yang sudah diverifikasi. Tidak ada contoh kode yang dianggap mewakili semua aset Sumbar.

Tidak ada API key atau langganan berbayar yang diperlukan untuk fungsi BMKG prototipe. Peta awal memakai OpenStreetMap 2D; integrasi Google, radar/nowcast, petir premium, SCADA dan penyimpanan terpusat belum aktif. Akses sumber bisa gagal atau dibatasi; dashboard menampilkan kondisi tersebut dan tidak menggantinya dengan data sintetis secara diam-diam.

## Verifikasi

```powershell
node node_modules/typescript/bin/tsc --noEmit
node --experimental-strip-types --test tests/disaster-model.test.mjs
npm run build
```

Sembilan pengujian mencakup masa berlaku CAP, cancellation/update, status Test, lat/lon CAP, pilihan bahasa, titik pada batas, ruas melintas polygon, koridor, lubang polygon, dan validasi impor. Fixture bukan data PLN.

## Konektor

`GET /api/bmkg?product=nowcast|quake|forecast|cap` memakai daftar tujuan BMKG tetap. Forecast memerlukan `adm4`; detail CAP memerlukan nama `file` yang berasal dari link feed. Tidak menerima URL upstream bebas. Cache dan pembatasan akses berlaku per instans, belum lintas replika. Untuk operasi berkelanjutan implementasikan collector pusat, penyimpanan dan antrean alarm sesuai rancangan.

## Struktur

- `components/disaster-dashboard.tsx`: dashboard, peta, import dan polling.
- `lib/disaster-model.ts`: parsing CAP, validasi geometri dan irisan spasial.
- `app/api/bmkg/route.ts`: konektor sumber resmi.
- `RANCANGAN-DCC.md`: arsitektur produksi dan opsi premium.
- `.openai/hosting.json`: identitas Site privat; jangan menggantinya ketika memperbarui.
