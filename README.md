# Coretax Integration (PajakMu) untuk Odoo 19

Modul Odoo yang mengirim **e-Faktur Keluaran** dan **Bukti Potong PPh** dari Odoo ke DJP (Coretax) melalui
**PajakMu**. Hasil dari DJP — status, Nomor Faktur Pajak, kode persetujuan — kembali ke Odoo secara otomatis.

Modul ada di folder [`coretax_integration/`](coretax_integration/).

## Apa yang bisa dilakukan

| Fitur | Keterangan |
|---|---|
| Kirim e-Faktur | Dari faktur pelanggan (tombol **Kirim e-Faktur**), tombol **Kirim** per baris, atau banyak sekaligus (centang di **e-Faktur › Siap dikirim**). Pilihannya: langsung kirim ke DJP, atau simpan sebagai draft di PajakMu untuk disetujui di sana. |
| Pemeriksaan sebelum kirim | Sebelum mengirim, setiap faktur diperiksa: NPWP/NIK pelanggan, kode transaksi, kode barang/jasa, mata uang IDR, kelompok pajak, tanggal. Pesannya ditulis dalam bahasa yang mudah dipahami. |
| Status otomatis | Odoo menanyakan kabar terbaru ke PajakMu setiap 5 menit. Bisa juga memakai webhook bertanda tangan HMAC supaya lebih cepat. Status dan nomor faktur pajak tampil di faktur Odoo. |
| Perlu tindakan | e-Faktur yang ditolak DJP atau gagal dikirim dikumpulkan di satu daftar, lengkap dengan alasannya dan tombol **Buat ulang e-Faktur**. Penanggung jawab faktur juga menerima **aktivitas** "Perbaiki e-Faktur" di Odoo. |
| Batal e-Faktur | Tombol **Batalkan e-Faktur** di faktur yang sudah disetujui meminta alasan (minimal 5 karakter). Status menjadi **Proses batal**, lalu **Dibatalkan**. Ditahan selama e-Faktur masih punya nota retur aktif. |
| Ganti e-Faktur | Tombol **Ganti e-Faktur** membuka faktur untuk diedit; pengiriman berikutnya berupa faktur pengganti dengan NSFP asli. Setelah disetujui, e-Faktur lama berstatus **Diganti**. |
| PDF faktur pajak | Tombol **Unduh PDF Faktur Pajak** menyimpan PDF resmi sebagai satu lampiran di faktur, diperbarui setiap kali diunduh. |
| Kode transaksi 07/08/10 | e-Faktur bisa dikirim dengan kode 07 dan 08 (keterangan tambahan dari tab Info Lainnya; 07: 1-31, 08: 1-10) dan kode 10. |
| Nota retur | Dari nota kredit yang sudah diposting (pelanggan atau vendor), tombol **Kirim nota retur** membuka wizard. Nilai DPP/PPN/PPnBM dihitung seperti PajakMu dan bisa diubah. Status kembali otomatis; **Batalkan nota retur** meminta alasan. Nota kredit terkunci selama nota retur aktif. |
| Lawan transaksi | Pelanggan dan vendor ditautkan ke *Lawan Transaksi* PajakMu lewat NPWP (**Cocokkan semua**, wizard **Cocokkan dengan PajakMu**, atau otomatis saat mengirim). Yang belum ada bisa **didaftarkan dari Odoo** di **Data Master › Lawan Transaksi** (satu per satu atau massal); NPWP yang ternyata sudah terdaftar langsung ditautkan. Lawan transaksi yang sudah cocok bisa diperbarui lewat tombol **Perbarui di PajakMu** (di kontak, atau aksi daftar). Bila diaktifkan, kontak baru juga didaftarkan otomatis saat mengirim e-Faktur/bukti potong. |
| Pemetaan field | Data lawan transaksi (NPWP, NIK, nomor paspor/identitas lain, NITKU/TKU, nama, alamat, email) diambil dari field kontak standar Odoo, dan bisa diganti field lain termasuk field kustom/Studio di **Pengaturan › Koneksi PajakMu**. Pencocokan dan pemeriksaan memakai pemetaan yang sama. |
| Kesiapan data | **Data Master › Kesiapan data**: produk yang masih berkode barang/jasa umum 000000, satuan tanpa kode Coretax, dan pelanggan di faktur siap kirim yang belum punya NPWP/NIK - semuanya bisa diperbaiki langsung dari daftarnya. Angkanya juga tampil di Ringkasan. |
| Bukti Potong PPh | Dibuat dari tagihan vendor (tombol **Buat Bukti Potong**) lalu dikirim ke PajakMu sebagai draft. Penandatanganan dan pengiriman ke DJP dilakukan di PajakMu. |
| PPh di tagihan vendor | Pajak PPh adalah pajak pembelian bernilai negatif yang diberi **kode objek PPh (Coretax)**; empat pajak siap pakai tersedia untuk chart template `id` (PPh 23, PPh 23 SKB, PPh 4(2), dll). Tiap baris tagihan punya kolom **Kode objek** yang terisi otomatis dari pajaknya. Saat tagihan diposting, Odoo membuat satu bukti potong per kode objek (DPP dari baris, PPh dari jurnal). Pajak PPh tidak boleh dipasang di dalam pajak grup. |
| Pengaman bukti potong | Tagihan yang bukti potongnya sudah ada di PajakMu tidak bisa direset, dibatalkan, atau dihapus. Bukti potong yang belum dikirim ikut diperbarui saat diposting ulang dan dihapus bila tagihan dibatalkan atau dihapus. Nota kredit mengurangi DPP; bila bukti potong sudah di PajakMu, muncul peringatan dan aktivitas pembetulan per bukti potong. |
| Upgrade | Saat modul diupgrade ke 19.0.1.1.0, pajak PPh bawaan dimuat otomatis untuk perusahaan yang memakai chart template `id`. Tagihan lama tanpa PPh tidak berubah. |
| Ringkasan | Satu layar berisi jumlah faktur siap dikirim, sedang diproses, disetujui bulan ini, dan perlu tindakan, plus aktivitas terbaru. Ada juga panduan "Mulai di sini". |
| Riwayat Sinkronisasi | Setiap panggilan ke PajakMu tercatat selama 90 hari, termasuk ID Permintaan untuk dukungan PajakMu. API key tidak pernah ikut dicatat. |
| Pengaman | Faktur yang e-Faktur-nya sedang diproses atau sudah disetujui DJP tidak bisa dikembalikan ke draft atau dibatalkan di Odoo sebelum e-Faktur-nya dibatalkan di PajakMu. |

## Struktur menu

```
Coretax
├── Ringkasan
├── e-Faktur
│   ├── Siap dikirim          (faktur pelanggan ber-PPN yang belum punya e-Faktur)
│   ├── Semua e-Faktur        (daftar / kanban per status)
│   ├── Nota Retur            (nota retur keluaran dan masukan)
│   └── Perlu tindakan        (ditolak DJP / gagal dikirim)
├── Bukti Potong
├── Data Master
│   ├── Lawan Transaksi       (status di PajakMu, cocokkan / daftarkan)
│   ├── Kode Objek Pajak PPh
│   ├── Kode Barang/Jasa      (dari l10n_id_efaktur_coretax)
│   ├── Kode Satuan
│   └── Kesiapan data         (produk berkode umum, satuan tanpa kode, pelanggan tanpa NPWP/NIK)
└── Pengaturan                (khusus Administrator Coretax)
    ├── Koneksi PajakMu
    └── Riwayat Sinkronisasi
```

Hak akses ada dua tingkat:
- **Pengguna:** semua pengguna *Invoicing* otomatis termasuk.
- **Administrator:** pengguna dengan hak *Administrator Akuntansi* otomatis termasuk.

## Syarat

- Odoo 19 dengan modul `account` dan `l10n_id_efaktur_coretax` (bawaan Odoo, ikut terpasang otomatis).
- Akun PajakMu dengan **paket Business atau lebih tinggi**, karena paket Starter tidak mencakup Public API.
- API key PajakMu yang dibuat oleh **Owner/Admin Pajak**, dengan izin `faktur:baca`, `faktur:tulis`, `faktur:kirim`,
  `bupot:*`, dan `status:baca`. Satu API key mewakili satu NPWP, jadi tiap perusahaan di Odoo butuh API key sendiri.
- Fitur batal, ganti, PDF, kode 07/08/10, nota retur, dan perbarui lawan transaksi butuh PajakMu versi terbaru
  (lihat **Upgrade**).
- Untuk mendaftarkan lawan transaksi dari Odoo, PajakMu harus sudah menyediakan `POST /api/v1/public/lawan-transaksi`
  (izin `faktur:tulis`).

## Instalasi

1. Tambahkan folder proyek ini ke `addons_path` Odoo, misalnya `addons_path = ...,/opt/coretax-integration`.
2. Restart Odoo, buka **Apps › Update Apps List**, cari **Coretax Integration (PajakMu)**, lalu klik **Install**.
3. Buka **Coretax › Pengaturan › Koneksi PajakMu** (juga ada di Akuntansi › Pengaturan › Coretax):
   1. Isi **Alamat PajakMu** dan **API Key**, simpan, lalu klik **Uji koneksi**.
   2. Pilih cara mengirim: manual dari faktur, atau otomatis saat faktur diposting.
   3. Opsional: pilih **Webhook**. Salin **Alamat webhook untuk PajakMu** ke PajakMu › Integrasi › Webhook, lalu
      tempel **Webhook Secret** (`whsec_…`) ke Odoo. Alamat Odoo harus bisa diakses dari internet (https).
4. Klik **Cocokkan semua pelanggan** (di Ringkasan atau form Koneksi). Yang tidak ketemu bisa didaftarkan dari
   **Data Master › Lawan Transaksi** (tombol **Daftarkan ke PajakMu**); yang ganda dicocokkan manual.
5. Pasang **Kode Barang/Jasa Coretax** di produk (tab Akuntansi). Produk tanpa kode akan memakai kode umum `000000`.

## Alur e-Faktur

```
Faktur Odoo (posted, ber-PPN)
   │  Kirim e-Faktur  →  pemeriksaan  →  POST /api/v1/public/faktur        (draft di PajakMu)
   │                                  →  POST /api/v1/public/faktur/{id}/kirim (antre ke DJP)
   ▼
PajakMu → PJAP → DJP
   │  hasil: event faktur.approved / faktur.rejected / faktur.cancelled
   ▼
Odoo (polling /api/v1/public/events setiap 5 menit, atau webhook)
   → status, Nomor Faktur Pajak, kode persetujuan, alasan penolakan
```

- Data baris e-Faktur disusun dengan builder bawaan Odoo (`l10n_id_efaktur_coretax`), jadi angkanya sama dengan XML Coretax resmi.
  PajakMu menghitung ulang DPP dan PPN di sisinya.
- Bila PajakMu sibuk (HTTP 429/502/503/504), permintaan dicoba ulang sekali mengikuti `Retry-After`.
- Setiap `POST` memakai `Idempotency-Key` yang stabil per faktur dan versi. Kalau pengiriman diulang karena jaringan
  putus, PajakMu tidak membuat dokumen ganda.
- Setelah ditolak atau dibatalkan, **Buat ulang e-Faktur** membuat versi baru. Versi lama tetap tersimpan sebagai riwayat.

### Batalkan e-Faktur

Tombol **Batalkan e-Faktur** membuka wizard alasan (minimal 5 karakter). Status menjadi **Proses batal**, lalu
**Dibatalkan** saat DJP mengonfirmasi. Pembatalan ditahan selama e-Faktur masih punya nota retur aktif.

### Ganti e-Faktur (faktur pengganti)

Tombol **Ganti e-Faktur** membuka faktur untuk diedit. Saat dikirim lagi, Odoo mengirim faktur pengganti
(`fgPengganti`) dengan NSFP asli. Setelah disetujui, status e-Faktur lama menjadi **Diganti**. Dari Odoo, satu
e-Faktur bisa diganti satu kali; penggantian kedua dilakukan di PajakMu/DJP. Faktur tidak bisa dibatalkan selama
penggantian berjalan.

### PDF faktur pajak

Tombol **Unduh PDF Faktur Pajak** menyimpan PDF resmi sebagai satu lampiran di faktur dan memperbaruinya setiap
kali diunduh.

### Nota retur

- Dari nota kredit yang sudah diposting (pelanggan atau vendor), klik **Kirim nota retur**. Nilai DPP/PPN/PPnBM
  dihitung seperti PajakMu dan bisa diubah di wizard.
- **Keluaran:** faktur asal harus punya e-Faktur yang sudah disetujui.
- **Masukan:** isi **Nomor Faktur Pajak vendor** (13/16/17 digit, tab Info Lainnya) di tagihan vendor asal.
- Status kembali otomatis. **Batalkan nota retur** meminta alasan.
- Selama nota retur aktif, nota kredit tidak bisa direset, dibatalkan, atau dihapus.
- Daftarnya ada di tombol pintar **Nota Retur** dan menu **Coretax › e-Faktur › Nota Retur**. Di aplikasi PajakMu
  ada halaman Nota Retur.

## Batasan saat ini (dari sisi PajakMu Public API)

Hal berikut belum tersedia di Public API PajakMu, jadi dilakukan langsung di aplikasi PajakMu. Tombol **Buka di PajakMu**
membuka dokumennya.

- penandatanganan dan pengiriman Bukti Potong ke DJP
- Faktur Masukan dan SPT

Saat ini PajakMu terhubung ke DJP melalui PJAP PajakExpress dengan kontrak pra-Coretax (NSFP 13 digit). Format nomor
Coretax 17 digit menunggu spesifikasi dari PJAP. Modul ini hanya menampilkan nomor yang dikembalikan PajakMu.

## Upgrade

Fitur batal, ganti, PDF, kode 07/08/10, nota retur, dan perbarui lawan transaksi butuh PajakMu versi terbaru.
Urutannya: pasang PajakMu lebih dulu (termasuk migrasi 0017 dan 0018 di sisi PajakMu), baru upgrade modul Odoo
(versi 19.0.1.4.0). Dengan PajakMu lama, fitur-fitur ini dan kode 10 menampilkan pesan "butuh PajakMu versi terbaru".

## Pengujian

```bash
odoo-bin -d <db_uji> --addons-path=<odoo>/addons,<folder ini> -i coretax_integration \
  --test-tags /coretax_integration --stop-after-init
```

Tes memakai server PajakMu tiruan dan tidak menghubungi internet. Yang diuji:
- isi data yang dikirim (payload);
- pencocokan pelanggan otomatis;
- kirim ke DJP;
- event disetujui dan posisi event terakhir (cursor);
- pesan galat validasi;
- pelanggan yang belum terdaftar;
- kode transaksi yang belum didukung;
- wizard kirim;
- bukti potong;
- ringkasan;
- tanda tangan webhook.
