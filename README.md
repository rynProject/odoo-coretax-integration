# Coretax Integration (AksaTax) untuk Odoo 19

Modul Odoo yang mengirim **e-Faktur Keluaran** dan **Bukti Potong PPh** dari Odoo ke DJP (Coretax) melalui
**AksaTax**. Hasil dari DJP — status, Nomor Faktur Pajak, kode persetujuan — kembali ke Odoo secara otomatis.

Modul ada di folder [`coretax_integration/`](coretax_integration/).

## Apa yang bisa dilakukan

| Fitur | Keterangan |
|---|---|
| Kirim e-Faktur | Dari faktur pelanggan (tombol **Kirim e-Faktur**), tombol **Kirim** per baris, atau banyak sekaligus (centang di **e-Faktur › Siap dikirim**). Pilihannya: langsung kirim ke DJP, atau simpan sebagai draft di AksaTax untuk disetujui di sana. |
| Pemeriksaan sebelum kirim | Sebelum mengirim, setiap faktur diperiksa: NPWP/NIK pelanggan, kode transaksi, kode barang/jasa, mata uang IDR, kelompok pajak, tanggal. Pesannya ditulis dalam bahasa yang mudah dipahami. |
| Status otomatis | Odoo menanyakan kabar terbaru ke AksaTax setiap 5 menit. Bisa juga memakai webhook bertanda tangan HMAC supaya lebih cepat. Status dan nomor faktur pajak tampil di faktur Odoo. |
| Perlu tindakan | e-Faktur yang ditolak DJP atau gagal dikirim dikumpulkan di satu daftar, lengkap dengan alasannya dan tombol **Buat ulang e-Faktur**. Penanggung jawab faktur juga menerima **aktivitas** "Perbaiki e-Faktur" di Odoo. |
| Lawan transaksi | Pelanggan dan vendor ditautkan ke *Lawan Transaksi* AksaTax lewat NPWP (**Cocokkan semua**, wizard **Cocokkan dengan AksaTax**, atau otomatis saat mengirim). Yang belum ada bisa **didaftarkan dari Odoo** di **Data Master › Lawan Transaksi** (satu per satu atau massal); NPWP yang ternyata sudah terdaftar langsung ditautkan. Bila diaktifkan, kontak baru juga didaftarkan otomatis saat mengirim e-Faktur/bukti potong. |
| Pemetaan field | Data lawan transaksi (NPWP, NIK, nomor paspor/identitas lain, NITKU/TKU, nama, alamat, email) diambil dari field kontak standar Odoo, dan bisa diganti field lain termasuk field kustom/Studio di **Pengaturan › Koneksi AksaTax**. Pencocokan dan pemeriksaan memakai pemetaan yang sama. |
| Kesiapan data | **Data Master › Kesiapan data**: produk yang masih berkode barang/jasa umum 000000, satuan tanpa kode Coretax, dan pelanggan di faktur siap kirim yang belum punya NPWP/NIK - semuanya bisa diperbaiki langsung dari daftarnya. Angkanya juga tampil di Ringkasan. |
| Bukti Potong PPh | Dibuat dari tagihan vendor (tombol **Buat Bukti Potong**) lalu dikirim ke AksaTax sebagai draft. Penandatanganan dan pengiriman ke DJP dilakukan di AksaTax. |
| PPh di tagihan vendor | Pajak PPh adalah pajak pembelian bernilai negatif yang diberi **kode objek PPh (Coretax)**; empat pajak siap pakai tersedia untuk chart template `id` (PPh 23, PPh 23 SKB, PPh 4(2), dll). Tiap baris tagihan punya kolom **Kode objek** yang terisi otomatis dari pajaknya. Saat tagihan diposting, Odoo membuat satu bukti potong per kode objek (DPP dari baris, PPh dari jurnal). Pajak PPh tidak boleh dipasang di dalam pajak grup. |
| Pengaman bukti potong | Tagihan yang bukti potongnya sudah ada di AksaTax tidak bisa direset, dibatalkan, atau dihapus. Bukti potong yang belum dikirim ikut diperbarui saat diposting ulang dan dihapus bila tagihan dibatalkan atau dihapus. Nota kredit mengurangi DPP; bila bukti potong sudah di AksaTax, muncul peringatan dan aktivitas pembetulan per bukti potong. |
| Upgrade | Saat modul diupgrade ke 19.0.1.1.0, pajak PPh bawaan dimuat otomatis untuk perusahaan yang memakai chart template `id`. Tagihan lama tanpa PPh tidak berubah. |
| Ringkasan | Satu layar berisi jumlah faktur siap dikirim, sedang diproses, disetujui bulan ini, dan perlu tindakan, plus aktivitas terbaru. Ada juga panduan "Mulai di sini". |
| Riwayat Sinkronisasi | Setiap panggilan ke AksaTax tercatat selama 90 hari, termasuk ID Permintaan untuk dukungan AksaTax. API key tidak pernah ikut dicatat. |
| Pengaman | Faktur yang e-Faktur-nya sedang diproses atau sudah disetujui DJP tidak bisa dikembalikan ke draft atau dibatalkan di Odoo sebelum e-Faktur-nya dibatalkan di AksaTax. |

## Struktur menu

```
Coretax
├── Ringkasan
├── e-Faktur
│   ├── Siap dikirim          (faktur pelanggan ber-PPN yang belum punya e-Faktur)
│   ├── Semua e-Faktur        (daftar / kanban per status)
│   └── Perlu tindakan        (ditolak DJP / gagal dikirim)
├── Bukti Potong
├── Data Master
│   ├── Lawan Transaksi       (status di AksaTax, cocokkan / daftarkan)
│   ├── Kode Objek Pajak PPh
│   ├── Kode Barang/Jasa      (dari l10n_id_efaktur_coretax)
│   ├── Kode Satuan
│   └── Kesiapan data         (produk berkode umum, satuan tanpa kode, pelanggan tanpa NPWP/NIK)
└── Pengaturan                (khusus Administrator Coretax)
    ├── Koneksi AksaTax
    └── Riwayat Sinkronisasi
```

Hak akses ada dua tingkat:
- **Pengguna:** semua pengguna *Invoicing* otomatis termasuk.
- **Administrator:** pengguna dengan hak *Administrator Akuntansi* otomatis termasuk.

## Syarat

- Odoo 19 dengan modul `account` dan `l10n_id_efaktur_coretax` (bawaan Odoo, ikut terpasang otomatis).
- Akun AksaTax dengan **paket Business atau lebih tinggi**, karena paket Starter tidak mencakup Public API.
- API key AksaTax yang dibuat oleh **Owner/Admin Pajak**, dengan izin `faktur:baca`, `faktur:tulis`, `faktur:kirim`,
  `bupot:*`, dan `status:baca`. Satu API key mewakili satu NPWP, jadi tiap perusahaan di Odoo butuh API key sendiri.
- Untuk mendaftarkan lawan transaksi dari Odoo, AksaTax harus sudah menyediakan `POST /api/v1/public/lawan-transaksi`
  (izin `faktur:tulis`).

## Instalasi

1. Tambahkan folder proyek ini ke `addons_path` Odoo, misalnya `addons_path = ...,/opt/coretax-integration`.
2. Restart Odoo, buka **Apps › Update Apps List**, cari **Coretax Integration (AksaTax)**, lalu klik **Install**.
3. Buka **Coretax › Pengaturan › Koneksi AksaTax** (juga ada di Akuntansi › Pengaturan › Coretax):
   1. Isi **Alamat AksaTax** dan **API Key**, simpan, lalu klik **Uji koneksi**.
   2. Pilih cara mengirim: manual dari faktur, atau otomatis saat faktur diposting.
   3. Opsional: pilih **Webhook**. Salin **Alamat webhook untuk AksaTax** ke AksaTax › Integrasi › Webhook, lalu
      tempel **Webhook Secret** (`whsec_…`) ke Odoo. Alamat Odoo harus bisa diakses dari internet (https).
4. Klik **Cocokkan semua pelanggan** (di Ringkasan atau form Koneksi). Yang tidak ketemu bisa didaftarkan dari
   **Data Master › Lawan Transaksi** (tombol **Daftarkan ke AksaTax**); yang ganda dicocokkan manual.
5. Pasang **Kode Barang/Jasa Coretax** di produk (tab Akuntansi). Produk tanpa kode akan memakai kode umum `000000`.

## Alur e-Faktur

```
Faktur Odoo (posted, ber-PPN)
   │  Kirim e-Faktur  →  pemeriksaan  →  POST /api/v1/public/faktur        (draft di AksaTax)
   │                                  →  POST /api/v1/public/faktur/{id}/kirim (antre ke DJP)
   ▼
AksaTax → PJAP → DJP
   │  hasil: event faktur.approved / faktur.rejected / faktur.cancelled
   ▼
Odoo (polling /api/v1/public/events setiap 5 menit, atau webhook)
   → status, Nomor Faktur Pajak, kode persetujuan, alasan penolakan
```

- Data baris e-Faktur disusun dengan builder bawaan Odoo (`l10n_id_efaktur_coretax`), jadi angkanya sama dengan XML Coretax resmi.
  AksaTax menghitung ulang DPP dan PPN di sisinya.
- Bila AksaTax sibuk (HTTP 429/502/503/504), permintaan dicoba ulang sekali mengikuti `Retry-After`.
- Setiap `POST` memakai `Idempotency-Key` yang stabil per faktur dan versi. Kalau pengiriman diulang karena jaringan
  putus, AksaTax tidak membuat dokumen ganda.
- Setelah ditolak atau dibatalkan, **Buat ulang e-Faktur** membuat versi baru. Versi lama tetap tersimpan sebagai riwayat.

## Batasan saat ini (dari sisi AksaTax Public API)

Hal berikut belum tersedia di Public API AksaTax, jadi dilakukan langsung di aplikasi AksaTax. Tombol **Buka di AksaTax**
membuka dokumennya.

- mengubah Lawan Transaksi yang sudah ada
- membatalkan atau mengganti e-Faktur, nota retur, dan unduh PDF faktur pajak
- penandatanganan dan pengiriman Bukti Potong ke DJP
- Faktur Masukan dan SPT
- kode transaksi **07/08** (butuh keterangan tambahan) dan **10**

Saat ini AksaTax terhubung ke DJP melalui PJAP PajakExpress dengan kontrak pra-Coretax (NSFP 13 digit). Format nomor
Coretax 17 digit menunggu spesifikasi dari PJAP. Modul ini hanya menampilkan nomor yang dikembalikan AksaTax.

## Pengujian

```bash
odoo-bin -d <db_uji> --addons-path=<odoo>/addons,<folder ini> -i coretax_integration \
  --test-tags /coretax_integration --stop-after-init
```

Tes memakai server AksaTax tiruan dan tidak menghubungi internet. Yang diuji:
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
