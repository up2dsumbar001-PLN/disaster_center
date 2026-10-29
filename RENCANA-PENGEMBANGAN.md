# Rencana Pengembangan Web App Disaster Information Center

Versi 0.3 · 28 Oktober 2026 · Cakupan Sumatera Barat · Marco ditunda sesuai instruksi pengguna.

Rencana ini memprioritaskan aset yang dapat dipercaya dan dokumentasi dampak yang tersimpan. Cuaca visual dapat dimulai melalui Windy setelah uji lisensi/kompatibilitas; analisis radar/nowcast otomatis membutuhkan jalur data tersendiri. Estimasi merupakan perkiraan upaya per tahap, bukan komitmen tanggal selesai atau izin pembelian.

## 1 Hasil pembaruan saat ini

Dokumen PRD, rencana, pilihan layanan dan kontrak data telah diperbarui. Prototipe sebelumnya tetap memiliki peta, impor aset per sesi, BMKG dan analisis irisan CAP. Sinkronisasi ArcGIS, editor kejadian, penyimpanan foto, akun dan Windy belum diklaim sudah berjalan. Semua berkas proyek tetap berada di root `C:\Users\DCC-KANAN\Documents\disaster_center`.

Baseline ArcGIS menggantikan Google Earth/KML sebagai sumber utama yang direncanakan. KML/KMZ tetap dapat dipakai untuk bootstrap/cadangan. Referensi Marco sudah dicatat di PRD; tidak ada pengerjaan konektornya pada tahap awal.

## 2 Urutan tahap

| Tahap | Estimasi awal | Hasil | Dependensi utama |
|---|---|---|---|
| A Validasi sumber dan skema | 1 sampai 2 minggu | Field mapping ArcGIS, ID gardu dan sumber pelanggan | Metadata/sampel serta pemilik GIS |
| B Database dan hak akses | 1 sampai 2 minggu | Data permanen, autentikasi, Admin/User/Guest dan audit | Host, kebijakan akses dan penyimpanan |
| C Sinkronisasi ArcGIS dan BMKG | 2 sampai 3 minggu | Collector, cache dan kesehatan sumber | Endpoint, izin baca dan pengujian kuota |
| D Tagging polygon dan foto | 2 sampai 3 minggu | Dokumentasi kejadian dan summary gardu/pelanggan | Snapshot aset/pelanggan dan object storage |
| E Windy dan lapisan cuaca | 1 sampai 2 minggu setelah akses siap | Pilot angin, awan, AQI dan overlay aset | Produk/key resmi, kompatibilitas dan lisensi |
| F Radar nowcast dan petir rinci | 2 sampai 4 minggu evaluasi setelah sampel tersedia | Bukti kualitas, adapter dan paparan mendatang | Feed mesin, izin analisis dan coverage Sumbar |
| G UAT dan pilot DCC | 2 sampai 4 minggu atau sampai kejadian memadai | Pengujian operasi, tuning dan serah terima | Pengguna uji dan prosedur fallback |
| H Marco | Ditunda, belum diestimasi | Status hijau/merah yang terhubung ke ID gardu | Instruksi pengguna untuk melanjutkan dan sumber yang sah |

Tahap dapat tumpang tindih sesuai kapasitas tim. Waktu tunggu data/lisensi dan sampel kejadian tidak dapat dihitung sebagai jam coding. Tahap F tidak boleh menunda manfaat tahap D bila feed belum siap.

## 3 Tahap A Sumber aset dan pelanggan

Inventarisasi layer gardu, tiang, jalur dan penyulang ArcGIS. Periksa service metadata, jumlah record, ID stabil, sistem koordinat, pagination, edit tracking, hak akses dan cara mendeteksi delete. Jangan mengasumsikan semua versi ArcGIS menyediakan kemampuan sinkronisasi yang sama.

Pastikan apakah jumlah pelanggan tersedia sebagai field gardu atau perlu sumber bisnis lain. Periksa tanggal berlaku, duplikasi, perpindahan pelanggan dan arti angka aktif/terdaftar. Buat contoh gardu dengan data lengkap, kosong, nol valid dan ID yang tidak cocok. Tetapkan salinan snapshot awal untuk pengujian.

**Gerbang A:** pemetaan identitas dan arti jumlah pelanggan disetujui GIS/DCC. Selisih/unknown didaftarkan, bukan diisi tebakan.

## 4 Tahap B Penyimpanan dan akses

Bangun database spasial dan penyimpanan objek sesuai host organisasi. Implementasikan akun, reset/penonaktifan pengguna melalui prosedur organisasi, peran Admin/User/Guest dan otorisasi API. User memiliki semua fitur operasional; administrasi akun/konektor hanya Admin.

Sediakan jalur view tanpa login untuk guest dengan lapisan yang dipilih organisasi. Uji bahwa endpoint guest tidak membocorkan data di luar tampilan yang dipublikasikan dan tidak bisa dipakai untuk write. Site hosted saat ini tetap privat sampai rancangan akses diputuskan; perubahan ini tidak otomatis mempublikasikannya.

**Gerbang B:** data dan audit bertahan setelah restart, role enforcement dan backup/restore dasar lulus.

## 5 Tahap C Collector dan sinkronisasi

Lakukan full sync ArcGIS dengan staging, validasi dan publish snapshot secara atomik. Tambahkan pembaruan inkremental bila capability tersedia serta rekonsiliasi penghapusan. Retry tidak boleh menggandakan aset; halaman yang gagal diambil tidak dianggap tidak ada.

Pertahankan BMKG sebagai sumber resmi; buat cache/kuota bersama dan pemrosesan Update/Cancel. Monitor umur aset dan cuaca terpisah. Uji timeout, token expired, HTTP 403/429/5xx, skema berubah dan respons kosong yang tidak valid.

**Gerbang C:** snapshot ArcGIS dan feed cuaca dapat ditelusuri; sumber gagal tidak menghasilkan klaim kondisi aman atau status gardu buatan.

## 6 Tahap D Fitur kejadian dan analisis

Bangun editor titik tagging dan polygon beserta revisinya. Simpan kategori, waktu, deskripsi, status dan hubungan ke kejadian. Implementasikan foto, thumbnail, metadata dan izin tampil. Uji perubahan oleh beberapa pengguna agar edit tidak menimpa diam-diam.

Kerjakan summary gardu/pelanggan di server berdasarkan dataset lengkap. Uji titik di batas, lubang polygon, MultiPolygon, polygon overlap, duplikasi ID dan pelanggan belum diketahui. Hasil menyimpan versi data; rekap terkini dan snapshot laporan lama harus dibedakan. Selama Marco ditunda, angka pelanggan padam tidak ditampilkan sebagai fakta.

**Gerbang D:** buat kejadian, unggah foto, simpan polygon, refresh dan buka dari akun lain berhasil; contoh hitungan di PRD sesuai; guest hanya view.

## 7 Tahap E Windy untuk visualisasi

Mulai dari proof of concept pada modul peta terisolasi. Periksa perbedaan Leaflet/CSS yang digunakan SDK Windy dengan prototipe. Uji marker aset, gambar/edit polygon, garis jaringan, pilihan waktu, legenda, panah angin dan penggunaan memori pada monitor control room.

Verifikasi lapisan dan lisensi yang berlaku pada produk/key. Bedakan cloud cover dari Cb, prakiraan petir dari kejadian petir, serta polutan dari indeks AQI. Jika Map API hanya cukup untuk visualisasi, jangan memakainya sebagai mesin alert berbasis data yang tidak didokumentasikan. Pertahankan analisis polygon aset pada database sendiri.

**Gerbang E:** tidak ada konflik peta/editor dan setiap lapisan menampilkan sumber/waktu/arti yang benar. Tidak menggunakan data testing sebagai operasi. Keputusan membeli produk dibuat setelah gap terhadap kebutuhan diketahui.

## 8 Tahap F Jalur analisis radar dan nowcast

Untuk kebutuhan ETA atau paparan sel hujan, prioritaskan penjajakan feed resmi BMKG. Bandingkan Tomorrow.io dan Meteomatics bila jalur itu tidak memenuhi kebutuhan. Xweather dievaluasi sebagai pelengkap petir aktual. Windy tetap kandidat visualisasi kecuali vendor mengonfirmasi produk data dan hak analisis yang memadai.

Gunakan kejadian, wilayah dan definisi metrik yang sama untuk vendor. Periksa coverage pesisir/berbukit, metadata no-data, interval frame, proyeksi, latensi dan jendela histori. Pilih berdasarkan kualitas serta total biaya, bukan hanya tampilan. Ranking rinci terdapat dalam [BIAYA-DAN-LAYANAN.md](./BIAYA-DAN-LAYANAN.md).

**Gerbang F:** data, hak pemakaian dan kualitas lokal terbukti; horizon serta ketidakpastian ditampilkan; tidak ada klaim titik sambaran petir pasti.

## 9 Tahap G UAT dan penerimaan operasi

Uji skenario lengkap dari pembaruan aset sampai kejadian ditutup. Sertakan kegagalan sumber, pelanggan kosong, foto gagal, user kehilangan hak, polygon direvisi, CAP dibatalkan dan pemulihan layanan. Ukur performa menggunakan jumlah aset dan pengguna yang representatif.

Latihan operator menggunakan data sintetis berlabel atau replay yang berizin. Catat alarm palsu, missed event, lead time, pengalaman operator, biaya dan temuan. Serahkan panduan operasi, fallback, retensi, backup, key dan dukungan. Aktivasi operasi terbatas mengikuti penerimaan pemilik proses.

## 10 Prioritas backlog terdekat

1. Terima URL/metadata layer ArcGIS dan contoh field gardu/penyulang.
2. Pastikan sumber jumlah pelanggan dan aturan gardu unik.
3. Tetapkan host database/foto dan cakupan view guest.
4. Bangun data permanen serta role enforcement.
5. Sinkronisasikan aset/BMKG, kemudian tagging, polygon dan foto.
6. Uji Windy dengan kebutuhan lapisan nyata, baru tentukan pembelian.
7. Tambahkan radar/nowcast analitis dan petir berdasarkan hasil evaluasi.

Marco tetap ditunda. Tidak ada perubahan firewall, pengaktifan server LAN, pembelian langganan atau pengiriman permintaan vendor pada pembaruan dokumen ini.
