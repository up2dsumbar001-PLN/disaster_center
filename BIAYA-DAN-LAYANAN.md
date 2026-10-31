# Peta Akses Jaringan dan Pilihan Layanan Cuaca GRIDCAST

> Catatan 29 Oktober 2026: aplikasi kini bernama **GRIDCAST** (Weather Intelligence & Grid Asset Monitoring, PLN UP2D Sumatera Barat). Isi dokumen ini ditulis saat aplikasi masih bernama Disaster Information Center; istilah teknis seperti database `disaster_center` tetap.

Tanggal pemeriksaan sumber publik 28 Oktober 2026. Harga dalam USD sebelum pajak, kurs, biaya pengembangan, hosting dan dukungan tambahan. Ini dasar perencanaan, bukan penawaran vendor atau persetujuan pembelian.

Pembaruan versi 0.3: Windy ditambahkan sebagai kandidat prioritas visualisasi, ArcGIS sebagai sumber aset, dan konektor Marco ditunda. Harga Windy pada bagian 8 berdenominasi EUR. Urutan implementasi hemat tersedia pada bagian 9; urutan untuk radar analitis dibedakan agar produk dengan fungsi berbeda tidak dianggap saling menggantikan.

## 1 Dampak menolak dialog Node js

Jika dialog meminta izin jaringan Private/Public, kemungkinan yang dimaksud adalah Windows Firewall. Menolaknya biasanya mencegah koneksi masuk dari perangkat lain ke server Node.js pada profil jaringan tersebut. Server lokal pada loopback dan permintaan keluar ke API umumnya tetap dapat berjalan, kecuali ada aturan organisasi lain. Ini tidak otomatis memblokir seluruh akses internet Node.js. Perilaku dasar Windows adalah memblokir trafik masuk yang tidak diizinkan dan mengizinkan trafik keluar kecuali terkena aturan blok. [Microsoft Windows Firewall](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/).

Jika dialog sebenarnya UAC yang bertanya apakah aplikasi boleh membuat perubahan pada perangkat, penolakan membatalkan operasi yang membutuhkan hak administrator. Tanpa teks dialog, tidak dapat dipastikan jenis dialog yang muncul. Penolakan tidak menghapus sumber proyek. Tidak ada perubahan firewall, pembukaan akses LAN atau server lokal baru yang dilakukan pada pekerjaan relokasi/dokumentasi ini.

Pada pengembangan berikutnya, akses dari komputer yang sama cukup memakai `127.0.0.1`. Akses dari video wall/PC lain memerlukan rancangan host, binding server, port dan aturan jaringan yang sesuai kebijakan TI/OT. Site privat yang sebelumnya di-host terpisah tidak bergantung pada izin koneksi masuk Node.js di PC ini.

## 2 Peta yang benar benar dipakai

Implementasi saat ini memakai pustaka Leaflet dan tile `https://tile.openstreetmap.org/{z}/{x}/{y}.png`. Ini basemap OpenStreetMap 2D, bukan Google Earth dan bukan citra satelit Google. Sumber peta diperiksa dari kode aplikasi setelah relokasi.

Data OSM terbuka, tetapi layanan tile publik tidak memiliki SLA. Atribusi dan kebijakan pemakaian tetap berlaku; unduhan massal/offline dari tile publik tidak diizinkan. Untuk ruang kontrol 24/7, gunakan layanan ber-SLA atau peta internal dengan hak pemakaian yang sesuai. [Kebijakan tile OSM](https://operations.osmfoundation.org/policies/tiles/).

## 3 Biaya Google Earth dan Google Maps berbeda

Untuk aplikasi Google Earth sendiri, paket **Standard USD 0**, **Professional USD 75/pengguna/bulan**, dan **Professional Advanced USD 150/pengguna/bulan**. Paket berbayar menambah kemampuan Earth; tidak dengan sendirinya menghubungkan radar BMKG, menyinkronkan aset ke dashboard khusus atau mengaktifkan API Maps. Untuk memelihara baseline, evaluasi Standard terlebih dahulu. [Paket resmi Google Earth](https://developers.google.com/maps/documentation/earth/earth-plans).

Dashboard khusus yang memakai layanan peta Google memiliki pemakaian API terpisah. Tarif global awal yang diverifikasi:

| SKU API | Kuota gratis bulanan | Tarif tingkat awal setelah kuota |
|---|---:|---:|
| Dynamic Maps | 10.000 map loads | USD 7 per 1.000 |
| 2D Map Tiles | 100.000 tile requests | USD 0,60 per 1.000 |
| Photorealistic 3D Tiles | 1.000 billable events | USD 6 per 1.000 |

Tarif turun pada tingkat volume berikutnya. [Daftar harga Google Maps Platform](https://developers.google.com/maps/billing-and-pricing/pricing).

Contoh Dynamic Maps: 4 layar × 4 reload/hari × 30 hari = 480 loads, masih dalam kuota. Pada 20.000 loads, contoh biaya peta adalah USD 70/bulan. Contoh mengasumsikan tidak ada pemakaian lain pada SKU terkait.

Untuk 3D, ikuti unit dan pencatatan event SKU yang dipilih. Jangan menyamakan satu aset, satu jam layar menyala, satu frame cuaca, atau satu permintaan root/renderer dengan tagihan tanpa memeriksa metrik Cloud Billing. Dokumentasi kuota Map Tiles membedakan request root dan request renderer; jumlah yang dapat dipakai untuk perencanaan harus diukur pada implementasi aktual. [Usage and billing Map Tiles](https://developers.google.com/maps/documentation/tile/usage-and-billing), [definisi SKU](https://developers.google.com/maps/billing-and-pricing/sku-details).

Untuk pemakaian API, billing dan key tetap perlu disiapkan meskipun pemakaian berada dalam kuota gratis. Jangan memakai asumsi kredit tetap USD 200/bulan yang sudah berakhir. Gunakan monitoring serta kuota teknis; notifikasi anggaran sendiri tidak boleh dianggap sebagai pemutus pemakaian otomatis. [Panduan Map Tiles](https://developers.google.com/maps/documentation/tile/usage-and-billing).

**Rekomendasi desain:** Google Maps satelit/hybrid 2D untuk tampilan operasi bila citra Google dibutuhkan. Pertahankan satu instans peta dan perbarui overlay cuaca/aset; jangan memuat ulang seluruh peta setiap kali cuaca diperiksa. Tambahkan 3D melalui renderer seperti Cesium setelah cakupan dan kebutuhan hardware Sumbar diuji. Ini jalur aplikasi khusus dengan aset yang diimpor, bukan remote control sesi Earth dari tautan pengguna.

Paket Google Earth berbayar belum direkomendasikan untuk dibeli hanya demi overlay cuaca pada dashboard ini. Tentukan dulu fitur Earth tambahan yang benar-benar dibutuhkan; kebutuhan peta API dan cuaca dibeli/dikonfigurasi terpisah.

## 4 Rekomendasi radar dan nowcast

**Pilihan pertama adalah akses resmi BMKG.** BMKG menyediakan tampilan radar serta dashboard monitoring yang mencakup Sumatera Barat. Ketersediaan tampilan publik tidak membuktikan adanya feed mesin yang bisa langsung digunakan. Mintakan produk geospasial, format, metadata, frekuensi, histori, coverage, izin pemrosesan dan skema dukungan melalui jalur resmi. Harga khusus belum terverifikasi dan tidak boleh diasumsikan gratis. [Radar BMKG](https://www.bmkg.go.id/cuaca/radar), [monitoring Sumatera](https://publik.bmkg.go.id/event/sumatera/), [ketentuan integrasi BMKG](https://www.bmkg.go.id/ketentuan-penggunaan).

**Tomorrow.io adalah kandidat komersial untuk pilot nowcast.** Dokumentasi forecast menyebut prakiraan per menit untuk pengguna premium, satu jam ke depan. Produk Maps API menyediakan lapisan untuk visualisasi, sedangkan cakupan, sumber observasi/model dan hak komputasi spasial untuk Sumbar perlu dikonfirmasi. Enterprise ditawarkan melalui penawaran khusus; angka langganan yang cocok untuk DCC belum dapat ditetapkan dari halaman publik. [Dokumentasi forecast](https://docs.tomorrow.io/reference/weather-forecast), [produk dan paket](https://www.tomorrow.io/weather-api/).

**Meteomatics dapat menjadi pembanding teknis.** Dokumentasi `mix-radar` menyebut radar/nowcast, tetapi itu tidak membuktikan ketersediaan radar observasi yang memadai untuk Sumbar. Vendor harus menunjukkan sampel koordinat aset sebelum masuk daftar pembelian. [Sumber data Meteomatics](https://www.meteomatics.com/en/api/request/optional-parameters/data-source/).

## 5 Layanan petir untuk jaringan distribusi

**Vaisala Xweather direkomendasikan untuk evaluasi petir aktual**, sebagai pelengkap radar, bukan penggantinya. Endpoint petir mendokumentasikan cakupan global dan update real time. Uji deteksi CG/IC, timestamp, ketelitian lokal, batas pencarian, histori dan hak penggunaan sebelum memilih paket. [Dokumentasi Xweather Lightning](https://www.xweather.com/docs/weather-api/endpoints/lightning).

Kalkulator pay-as-you-go publik yang ditemukan dibatasi untuk US/Kanada. Karena lokasi pengguna Indonesia, tarif kalkulator tersebut tidak dipakai sebagai penawaran untuk proyek ini; minta harga regional/enterprise tertulis. [Batas wilayah kalkulator Xweather](https://www.xweather.com/pricing/weather-api-pay-as-you-go).

## 6 Spesifikasi permintaan penawaran

Kirim satu brief yang sama kepada kandidat setelah organisasi siap berkorespondensi: area Sumatera Barat, jumlah layar dan aset, horizon analisis, kebutuhan radar/nowcast vs petir, interval pembaruan, histori, hak perhitungan turunan, tampilan control room, retensi, SLA, dukungan dan mekanisme fallback. Tidak ada pesan atau permintaan penawaran yang dikirim dalam pekerjaan ini.

Minta metadata latensi dan no-data serta sampel pada koordinat jaringan pesisir dan medan berbukit. Evaluasi beberapa kejadian menggunakan observasi pembanding dan histori gangguan yang diizinkan. Metrik serta definisi kejadian ditentukan sebelum melihat hasil vendor. API cuaca titik yang murah belum tentu memenuhi kebutuhan melacak sel hujan menuju koridor jaringan.

## 7 Struktur anggaran yang perlu dipisahkan

| Komponen | Kondisi saat ini | Dasar anggaran berikutnya |
|---|---|---|
| Basemap | OSM publik pada prototipe | SLA/internal atau SKU Google yang dipilih |
| BMKG terbuka | Konektor sudah tersedia | Kepatuhan kuota, hosting collector dan pemeliharaan |
| Radar/nowcast khusus | Belum dikontrak | Penawaran resmi dan hasil pilot lokal |
| Petir aktual | Belum dikontrak | Penawaran regional dan kebutuhan histori |
| Hosting/database/audit | Prototipe, belum operasi | Desain TI, retensi, backup dan kapasitas |
| Pengembangan/integrasi | Belum ada penawaran biaya | PRD, jumlah aset, UAT dan akses sistem |

Tidak ada langganan, penambahan billing, pembelian API, perubahan sharing atau izin jaringan yang dilakukan sebagai bagian dari penyusunan catatan ini.

## 8 Windy sebagai kandidat visualisasi

Harga publik **Map Forecast Professional EUR 990/tahun** atau ekuivalen EUR 82,50/bulan bila dibagi rata, dengan pembayaran/ketentuan tahunan mengikuti vendor. Point Forecast Professional terpisah, juga EUR 990/tahun. Bila keduanya diperlukan, contoh total dasar EUR 1.980/tahun sebelum opsi model/pajak. Premium aplikasi Windy tidak dianggap pengganti kedua produk API. [Map Forecast pricing](https://api.windy.com/map-forecast/pricing), [Point Forecast pricing](https://api.windy.com/point-forecast/pricing).

Map Forecast mencantumkan lapisan angin, awan, satellite dan beberapa polutan. Radar tidak ditemukan pada daftar lapisan publik tersebut; jangan menganggapnya sudah tercakup. Opsi ECMWF memiliki biaya dan batas pemakaian tersendiri, sehingga perlu konfirmasi untuk tampilan guest. Ketersediaan pada Windy.com bukan bukti hak akses data mentah atau deteksi sambaran petir. [Daftar lapisan dan paket](https://api.windy.com/map-forecast/pricing).

Map API cocok untuk pilot tampilan, sementara Point API memberi data koordinat seperti wind dan CAMS/AQI menurut produk/model. Point Testing mengubah/mengacak data dan hanya untuk pengembangan, sehingga tidak dipakai sebagai sumber operasi. Histori Point Forecast juga tidak boleh diasumsikan tersedia. [Dokumentasi Point Forecast](https://api.windy.com/point-forecast/docs), [batas paket uji](https://api.windy.com/point-forecast/pricing).

Dokumentasi Map API menggunakan Leaflet 1.4.x, sedangkan prototipe memakai 1.9.4. Mulai dengan modul terisolasi untuk menguji SDK, CSS, editor polygon dan overlay ArcGIS. Ini penilaian upaya integrasi, bukan jaminan bahwa cukup memasukkan key lalu semua fitur berfungsi. [Dokumentasi Map Forecast](https://api.windy.com/map-forecast/docs).

## 9 Urutan prioritas berdasarkan kemudahan dan anggaran

Ranking berikut merupakan rekomendasi desain untuk kebutuhan pengguna, bukan bukti perbandingan harga vendor yang belum memberikan penawaran.

| Urutan | Pilihan | Kemudahan relatif | Anggaran dan alasan |
|---|---|---|---|
| 1 | BMKG terbuka dan peta/aset ArcGIS yang sudah dimiliki | Paling dekat dengan fondasi proyek | Tanpa membeli API cuaca baru untuk peringatan dasar; biaya operasi/akses ArcGIS tetap bergantung lisensi organisasi. Tidak memenuhi semua kebutuhan radar/Cb/petir |
| 2 | Windy Map Forecast Professional untuk visualisasi | Relatif cepat melalui SDK setelah uji kompatibilitas | Harga tetap publik; layak menjadi kandidat hemat untuk tampilan angin/awan, tanpa klaim termurah untuk seluruh kebutuhan |
| 3 | Windy Point Forecast hanya jika data numerik/AQI diperlukan | Menambah adapter, cache dan pengujian | Produk tambahan; jangan dibeli otomatis bila kebutuhan sudah dipenuhi sumber lain |
| 4 | Feed radar/nowcast khusus BMKG | Perlu akses, metadata dan pengolahan grid | Prioritas pertama yang dijajaki untuk radar analitis lokal, tetapi harga dan waktu pengadaan belum diketahui |
| 5 | Tomorrow.io | Integrasi dan evaluasi menengah | Kandidat nowcast komersial bila feed sebelumnya tidak mencukupi; harga/coverage/hak analisis perlu penawaran |
| 6 | Meteomatics | Integrasi geospasial menengah sampai tinggi | Pembanding setelah coverage Sumbar dibuktikan; harga belum dapat diperingkat lebih murah |

Xweather ditempatkan pada jalur pelengkap petir aktual, bukan diurutkan sebagai pengganti radar. Prioritaskan pilotnya bila lokasi kejadian petir dan korelasi gangguan menjadi kebutuhan mendesak. Harga regional Indonesia tetap memerlukan penawaran.

**Untuk tampilan cepat dengan pengeluaran terkendali:** mulai BMKG + aset ArcGIS + fungsi tagging/polygon/foto, kemudian uji Windy Map. **Untuk ETA hujan otomatis:** urutan penjajakan adalah feed resmi BMKG, kemudian Tomorrow.io, lalu Meteomatics. Windy masuk jalur ETA hanya jika produk data dan hak komputasinya dikonfirmasi; produk visual saja tidak cukup.

Pilihan termurah secara total belum bisa dipastikan tanpa jumlah aset, kuota, penyimpanan foto, retensi dan penawaran. Jangan menjumlahkan seluruh langganan sebagai kebutuhan awal: beli produk setelah kebutuhan yang belum terpenuhi terlihat pada pilot. Ketersediaan Cb, deteksi petir aktual dan AQI perlu diperiksa per produk, bukan diasumsikan datang dalam satu paket.

## 10 Anggaran fitur aplikasi dan keputusan tertunda

Database permanen, sinkronisasi ArcGIS, object storage foto dan autentikasi memerlukan anggaran di luar API cuaca. Perhitungkan ukuran foto/thumbnail, jumlah kejadian, retensi, backup, query spasial dan pengguna bersamaan. Memakai ArcGIS yang sudah tersedia dapat mengurangi pengadaan baru, tetapi batas kuota/kredit/hak akses organisasi tetap perlu diverifikasi.

Marco tidak dimasukkan ke jalur kritis atau biaya implementasi awal. Guest tanpa login adalah kebutuhan aplikasi yang direncanakan dan belum mengubah Site privat saat ini. Perubahan akses publik, pembelian API dan konektor baru tidak dilakukan pada pembaruan dokumentasi ini.
