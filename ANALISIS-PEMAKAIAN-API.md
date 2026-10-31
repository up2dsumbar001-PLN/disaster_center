# Analisa dan optimasi pemakaian API GRIDCAST

29 Oktober 2026. Prinsip: **satu panggilan diambil selengkap mungkin, disimpan di disk, lalu dipakai berulang sampai masa berlakunya habis.** Hanya server yang memanggil API luar, dan hasilnya dibagi ke semua dispatcher, jadi jumlah pengguna tidak menambah pemakaian.

## Ringkasan sebelum dan sesudah

| Sumber | Batas | Sebelum | Sesudah |
|---|---|---|---|
| Tomorrow.io | 25/jam, 500/hari (gratis) | 18/jam (432/hari). Per titik hanya 6 jam ke depan, 1 frame tile "now". Restart mengambil ulang semua. | ±19/jam (±461/hari). Per titik 120 jam + per menit 60 menit + 6 hari, tile 3 frame (sekarang, +1, +2 jam), nowcast tiap jam. Restart tidak memakai kuota. |
| Open-Meteo angin | 10.000 lokasi/hari (gratis, non-komersial) | Cache 15 menit di memori, 130 lokasi per ambil. Bila lapisan terus dibuka: ±12.480/hari (**melebihi batas**). | Deret 48 jam, disimpan 3 jam di disk: 8 × 130 = ±1.040/hari. Linimasa +1/+2 jam diambil dari cache yang sama. |
| Open-Meteo AQI | sama | Cache 30 menit, ≤40 lokasi: ±1.920/hari | Deret 48 jam, 3 jam: ±320/hari |
| BMKG CAP | 60 permintaan/menit per IP | Tiap 60 detik: RSS + semua berkas CAP yang terdaftar (bisa >10/menit) | RSS tiap 60 detik + **hanya berkas CAP baru**. Berkas CAP tidak berubah setelah terbit. |
| BMKG prakiraan desa | sama | Cache 30 menit di memori | Cache 3 jam di disk (BMKG memperbarui 2× sehari). Baris jam yang sudah lewat disembunyikan. |
| BMKG gempa | sama | 3 permintaan/5 menit | Tetap |
| RainViewer | 100/menit per IP | Metadata tiap 5 menit; tile diambil browser langsung | Tetap (catatan: semua browser intranet berbagi satu IP publik) |

## Tomorrow.io: cara kuota dipakai

Uji langsung dengan key Anda (29 Oktober) memastikan dua hal:
1. `GET /v4/weather/forecast` **tanpa** `timesteps` mengembalikan tiga timeline sekaligus untuk 1 panggilan: per menit (60 data), per jam (120 data, 5 hari), dan harian (6 data).
2. Tile peta bisa diminta untuk jam yang akan datang (`.../precipitationIntensity/2026-10-29T11:00:00Z.png`), bukan hanya `now`.

Karena itu server sekarang:
- **Titik UP3**: 1 panggilan per lokasi tiap 150 menit (dipanjangkan otomatis dari 120 menit). "Kondisi sekarang", prospek 6 jam, perkiraan jam mulai hujan, dan 5 hari **dihitung ulang dari cache** setiap kali diminta, jadi nilainya tetap mengikuti jam walau tidak ada panggilan baru. Data per menit memberi "hujan ±X menit lagi" selama masih berlaku (60 menit sejak diambil).
- **Tile hujan**: tiap frame (jam berlaku) diambil sekali dan disimpan sampai jamnya lewat. Pada kondisi tunak tiap jam hanya perlu 1 frame baru (+2 jam) = 10 tile, ditambah penyegaran nowcast 6 tile Sumbar sekali per jam. Bila batch terpotong kuota, tile yang sudah segar tidak diambil ulang.
- **Penjadwal berprioritas** (dievaluasi tiap 60 detik): tile detail Sumbar → titik UP3 terlama → tile Sumatra → nowcast. Berhenti di 22/jam dengan cadangan 2 panggilan untuk tombol uji Admin. Paling banyak 25 panggilan per siklus.
- **Interval aman otomatis**: bila Admin mengisi interval titik terlalu pendek, server memanjangkannya sehingga total harian tetap ≤470.

Rumus pada kondisi tunak (8 titik, interval 150 menit):

```
tile   = 10 frame baru/jam + 6 nowcast/jam  = 16 panggilan/jam
titik  = 8 lokasi × 60/150                  = 3,2 panggilan/jam
total  = 19,2/jam ≈ 461/hari  (batas aman 22/jam, 470/hari; batas Tomorrow 25 dan 500)
```

Batas yang menentukan adalah harian: 470/hari setara 19,6/jam, bukan 22/jam. Sisa 9/hari untuk uji koneksi Admin.

Saat cache masih kosong (pemasangan pertama atau setelah hapus `data\tomorrow`), pengisian penuh butuh ±44 panggilan, sehingga tersebar otomatis dalam 2–3 jam. Urutannya: Sumbar lebih dulu.

## Tempat cache

| Folder | Isi | Masa berlaku |
|---|---|---|
| `data\tomorrow\sites.json` | Timeline per titik UP3 (120 jam, 60 menit, 6 hari) | 2 jam lalu diperbarui; tetap bisa dipakai sampai timeline habis |
| `data\tomorrow\tiles\` | Tile PNG per jam berlaku | Sampai jamnya lewat (otomatis dihapus) |
| `data\tomorrow\usage.json` | Penghitung kuota per jam dan per hari (UTC) | Tahan restart |
| `data\cache\openmeteo\` | Deret 48 jam angin dan AQI | 3 jam |
| `data\cache\bmkg_forecast\` | Prakiraan BMKG per desa | 3 jam |

Semua folder aman dihapus; server akan mengisi ulang sesuai kuota.

## Memantau

**Administrasi → Konektor dan tampilan → Pemakaian API** menampilkan kuota Tomorrow.io jam ini dan hari ini, status jeda bila kena 429, jenis 20 panggilan terakhir, serta jumlah permintaan BMKG/Open-Meteo yang ke internet dibanding yang dilayani dari cache (sejak server terakhir dinyalakan).

## Yang sengaja tidak dilakukan

- Memakai beberapa akun/API key gratis untuk melewati batas: berisiko melanggar ketentuan layanan dan semua key bisa diblokir sekaligus.
- Menambah tile zoom 8 ke atas: 1 tile zoom 8 menutup ¼ luas tile zoom 7, sehingga Sumbar butuh ±12 tile per frame dan kuota tidak cukup.
