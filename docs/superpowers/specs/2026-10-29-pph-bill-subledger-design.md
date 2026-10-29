# PPh di Tagihan Vendor → Subledger Bukti Potong (coretax_integration)

- Tanggal: 2026-10-29
- Status: Draft untuk review
- Modul: `coretax_integration` (Odoo 19), versi naik ke `19.0.1.1.0`
- Referensi: `bt_withholding_tax` 18.0.1.1.1 (Metrocom Jaddi) - logika diadopsi, modul tidak dipasang

## 1. Latar belakang

`coretax.bupot` adalah subledger bukti potong yang dikirim ke AksaTax. Saat ini bupot dibuat
manual dari tagihan vendor dengan DPP = total untaxed seluruh bill (`account_move.py`,
`action_coretax_create_bupot`). Akibatnya:

- PPh tidak tercatat di jurnal (tidak ada Utang PPh, hutang usaha tidak dipotong).
- Bill dengan beberapa jenis PPh tidak bisa dipecah per kode objek.
- Tidak ada hubungan antara bupot dan baris bill / journal item, dan tidak ada pengaman saat
  bill di-reset atau dibatalkan.

`bt_withholding_tax` sudah punya mesin PPh lengkap (WHT Profile, pengakuan saat bill/payment,
subledger `account.wht.transaction`, sertifikat, reklasifikasi, tax valuation). Spec ini
mengambil bagian yang dibutuhkan untuk pengakuan **saat bill, sisi pembelian**, dan memakai
`coretax.bupot` sebagai subledgernya.

## 2. Tujuan dan kriteria sukses

Kasus acuan - satu tagihan vendor, tiga baris:

| Baris | Pajak | Kode objek | DPP |
|---|---|---|---|
| L1 | PPh 23 2% | 24-104-01 | 10.000.000 |
| L2 | PPh 23 2% | 24-104-01 | 5.000.000 |
| L3 | PPh 4(2) 10% | 28-402-01 | 20.000.000 |

Setelah bill diposting:

- Jurnal: Dr Beban 35.000.000; Cr Utang PPh 23 (21210030) 300.000; Cr Utang PPh 4(2)
  (21210050) 2.000.000; Cr Hutang usaha 32.700.000.
- Dua `coretax.bupot` draft (`new`):
  - 24-104-01, DPP 15.000.000, tarif 2%, PPh GL 300.000, sumber L1+L2.
  - 28-402-01, DPP 20.000.000, tarif 10%, PPh GL 2.000.000, sumber L3.
- Jika kode objek L2 diganti ke 24-104-03: Utang PPh 23 terpecah menjadi dua journal item
  (200.000 dan 100.000) dan terbentuk tiga bupot.
- Pembayaran memakai alur standar Odoo sebesar 32.700.000.

## 3. Keputusan

| Topik | Keputusan |
|---|---|
| Waktu pengakuan | Saat bill diposting saja. Tidak ada pengakuan saat payment. |
| Arah | Pembelian saja (perusahaan sebagai pemotong). |
| Kode objek | Default dari pajak PPh, bisa diubah per baris bill. |
| Granularitas bupot | Satu bupot per (bill, kode objek). |
| Pendekatan | Port ramping langsung ke `coretax_integration`; `bt_withholding_tax` tidak menjadi dependency. |

### Di luar scope

- Pengakuan saat payment, wizard Register Payment ber-PPh, partial payment PPh.
- PPh sisi penjualan (dipotong pelanggan, bupot diterima).
- WHT Profile, akun sementara/definitif dan reklasifikasi saat bupot dikonfirmasi.
- Tax valuation kurs KMK (bill valas memakai kurs akuntansi; DPP bisa dikoreksi manual).
- Penggabungan beberapa bill ke satu bupot.
- Pengiriman otomatis bupot ke AksaTax (tetap manual seperti sekarang).
- Import 138 kode objek dari seed `bt_withholding_tax`.

## 4. Desain

### 4.1 Konfigurasi pajak PPh

**`account.tax`**

- Field baru `coretax_tax_object_id` (Many2one `coretax.tax.object`, "Kode objek PPh (Coretax)").
- Pajak disebut *pajak PPh* bila field ini terisi.
- Constraint bila terisi: `type_tax_use = 'purchase'`, `amount_type = 'percent'`, `amount <= 0`.
- Tarif 0% = fasilitas (SKB/DTP): tidak menghasilkan journal item, tetapi tetap menghasilkan bupot.
- Akun Utang PPh diatur di repartition line standar Odoo.

**Pajak siap pakai untuk chart template `id`** (`@template('id', 'account.tax')` dan
`@template('id', 'account.tax.group')`, pola `l10n_tr_nilvera_einvoice_extended`):

| xmlid | Nama | Tarif | Kode objek | Akun pajak |
|---|---|---|---|---|
| `tax_pph23_2` | PPh 23 2% | -2 | 24-104-01 | `l10n_id_21210030` |
| `tax_pph23_skb` | PPh 23 SKB 0% | 0 | 24-104-01 | `l10n_id_21210030` |
| `tax_pph42_sewa_10` | PPh 4(2) Sewa 10% | -10 | 28-402-01 | `l10n_id_21210050` |
| `tax_pph26_20` | PPh 26 20% | -20 | 27-104-01 | `l10n_id_21210060` |

- Tax group baru `l10n_id_tax_group_pph` ("PPh"), sehingga total bill menampilkan PPh terpisah dari PPN.
- `post_init_hook` memuat pajak dan tax group ini untuk perusahaan yang sudah memakai chart
  `id`, melewati xmlid yang sudah ada.
- Kode objek pada template diambil dari `coretax_tax_object_data.xml` (xmlid
  `coretax_integration.tax_object_*`).

### 4.2 Kode objek per baris dan pemecahan journal item PPh

**`account.move.line.coretax_tax_object_id`** (Many2one `coretax.tax.object`, index):

- Baris produk: compute tersimpan, `readonly=False`, dari `tax_ids` → kode objek pajak PPh di
  baris. Bisa diganti user selama draft. Disalin saat credit note (`copy=True`).
- Constraint: maksimal satu pajak PPh per baris; jenis kode objek baris harus sama dengan jenis
  kode objek pajaknya (BPPU/BPNR).
- Journal item pajak: diisi lewat grouping key pajak.

**Override `account.tax`** (pola `hr_expense/models/account_tax.py`):

- `_prepare_base_line_for_taxes_computation`: tambahkan `coretax_tax_object_id` dari record.
- `_prepare_tax_line_for_taxes_computation`: sama, untuk journal item pajak yang sudah ada.
- `_prepare_base_line_tax_repartition_grouping_key`: tambahkan
  `coretax_tax_object_id = base_line kode objek` **hanya bila `tax_data['tax']` adalah pajak PPh**;
  selain itu `False`. PPN tidak ikut terpecah.
- `_prepare_tax_line_repartition_grouping_key`: tambahkan `coretax_tax_object_id` dari tax line,
  supaya journal item yang ada dikenali ulang saat hitung ulang.

Karena journal item pajak dibentuk dari `{**grouping_key, **values}`, nilai kode objek otomatis
tersimpan di journal item PPh.

### 4.3 Perubahan `coretax.bupot`

| Field | Perubahan |
|---|---|
| `origin` | Baru. Selection `bill` / `manual`, default `manual`, readonly. |
| `tarif` | Dari related `tax_object_id.tarif` menjadi field tersimpan. Default dari kode objek; bupot otomatis memakai `abs(tax.amount)`. |
| `source_line_ids` | Baru, compute non-stored: baris produk `move_id` (dan credit note-nya) dengan kode objek ini. |
| `tax_line_ids` | Baru, compute non-stored: journal item pajak PPh `move_id` dengan kode objek ini. |
| `pph_gl` | Baru, compute: −Σ `balance` `tax_line_ids` (bill = credit, credit note = debit, jadi otomatis netto). |
| `pph_selisih` | Baru, compute: `pph_gl - pph_estimasi`; peringatan bila \|selisih\| ≥ 1. |

- Link ke baris dan journal item sengaja non-stored dan dicari dari (bill, kode objek). Odoo
  menghapus dan membuat ulang journal item pajak saat hitung ulang, jadi foreign key tersimpan
  akan basi (masalah yang di `bt_withholding_tax` diatasi dengan menghapus `gl_link` sebelum AML
  ditulis ulang).
- `models.UniqueIndex("(move_id, tax_object_id) WHERE origin = 'bill' AND state NOT IN
  ('rejected', 'cancelled')")`.
- Untuk `origin = 'bill'`: `partner_id`, `jenis`, `tax_object_id`, `move_id` readonly.
  `jumlah_bruto`, tanggal, dokumen dasar, dan fasilitas bisa diedit selama `new`/`error`
  (tercatat di chatter). Sinkronisasi ulang hanya terjadi saat bill diposting ulang.
- `unlink` bupot `origin = 'bill'` diblokir kecuali dari proses internal (context
  `coretax_bupot_sync`).
- `_check_before_send`: tambahan "Tagihan vendor belum diposting" bila `origin = 'bill'` dan
  `move_id.state != 'posted'`.
- Bupot dari pajak 0%: `fasilitas` default `SKB`; nomor fasilitas sudah wajib di
  `_check_before_send`.

### 4.4 Alur transaksi

**Posting** (`account.move._post`, setelah `super()`, untuk `in_invoice`/`in_receipt`/`in_refund`):

1. Tentukan bill induk: bill itu sendiri, atau `reversed_entry_id` untuk `in_refund`.
2. Kelompokkan baris produk ber-pajak PPh dari bill induk dan semua credit note terposting-nya
   per kode objek.
3. DPP kelompok = Σ `balance` (IDR) baris sumber. Baris bill bersaldo debit dan baris credit note
   bersaldo credit, jadi hasilnya otomatis netto.
4. Untuk tiap kelompok, cari bupot aktif (bill induk, kode objek, `origin = 'bill'`):
   - tidak ada dan DPP > 0 → buat (`state = 'new'`);
   - ada, `new`/`error`, DPP > 0 → sinkronkan DPP, tarif, partner, jenis, tanggal pemotongan
     (`invoice_date`), dokumen dasar (`02`, `ref` atau `name`, `invoice_date`);
   - ada, `new`/`error`, DPP ≤ 0 → hapus, catat di chatter bill;
   - ada dan sudah di AksaTax → tidak diubah; bila nilainya berbeda (mis. karena credit note)
     posting pesan di chatter bupot + aktivitas "Lakukan pembetulan/pembatalan di AksaTax".
5. Bupot `new`/`error` milik bill induk yang kelompoknya sudah tidak ada → hapus, catat di chatter.

**Reset to draft / Cancel / Hapus bill** (pola `_coretax_guard`):

- Diblokir bila bill punya bupot `origin = 'bill'` dengan `aksatax_id` dan state bukan
  `rejected`/`cancelled`. Pesan meminta pembatalan bupot di AksaTax lebih dulu.
- Reset: bupot `new`/`error` dibiarkan, disinkronkan lagi saat posting ulang.
- Cancel dan hapus: bupot `new`/`error` dihapus, dicatat di chatter bill.
- Reset/cancel credit note: bill induk disinkronkan ulang (bupot `new`/`error` kembali ke DPP tanpa
  credit note itu).

**Pembayaran**: alur standar Odoo (bayar netto). Tidak ada logika tambahan.

**Tombol manual lama** `action_coretax_create_bupot` tetap ada untuk bill tanpa baris PPh.

### 4.5 Tampilan

- Form pajak: `coretax_tax_object_id` di tab Advanced Options, tampil untuk pajak pembelian.
- Baris tagihan vendor: kolom `coretax_tax_object_id` (optional show) untuk bill/receipt/credit
  note pembelian.
- Header bill: smart button "Bukti Potong (n)"; tombol "Buat Bukti Potong" hanya tampil bila
  bill tidak punya baris PPh.
- Form bupot: tab "Sumber" (asal, bill, tabel baris sumber, PPh GL vs perkiraan, selisih);
  field readonly sesuai 4.3.
- Daftar bupot: filter "Dari tagihan" / "Manual", group by kode objek.

## 5. Pengujian

Test `post_install` di Odoo 19 lokal, memakai `CoretaxTestCommon` dan `FakeAksaTax`:

1. Kasus acuan (bagian 2): jurnal dan dua bupot.
2. Kode objek L2 diganti → dua journal item PPh 23 dan tiga bupot.
3. PPN + PPh di baris yang sama → PPN tidak terpecah, DPP PPh tidak termasuk PPN.
4. Reset → posting ulang dengan nominal berubah → bupot sama (id tetap) terupdate; PPh dihapus
   → bupot terhapus.
5. Reset/cancel diblokir bila bupot sudah di AksaTax; cancel dengan bupot `new` → bupot terhapus.
6. Credit note sebagian → DPP netto; penuh → bupot terhapus; bupot terkirim → tidak berubah, ada
   aktivitas.
7. Pajak SKB 0% → bupot fasilitas SKB, PPh 0, tanpa journal item PPh.
8. Kirim bupot dari bill draft diblokir; payload AksaTax memakai DPP kelompok.
9. Pajak PPh tersedia untuk perusahaan Indonesia (template dan `post_init_hook`).

Test yang sudah ada harus tetap lulus.

## 6. Risiko

- **Override grouping key pajak** menyentuh perhitungan pajak inti Odoo. Mitigasi: hanya
  menambah satu key untuk pajak PPh; test 2 dan 3 menjaga PPN tetap utuh.
- **Pembulatan**: GL memakai pembulatan Odoo (0,01), perkiraan PPh DJP dibulatkan ke bawah.
  Selisih ditampilkan, tidak dipaksa sama.
- **Bill valas**: DPP memakai kurs akuntansi, bukan kurs KMK. DPP bisa dikoreksi manual sebelum
  dikirim.
