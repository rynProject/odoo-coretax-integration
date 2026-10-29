# Paket e-Faktur: Lawan Transaksi, Batal, Pengganti, PDF, Kode 07/08/10, Nota Retur

- Tanggal: 2026-10-29
- Status: Draft untuk review
- Repo yang disentuh:
  - `~/aksatax-be` (branch dasar `development`): Public API, modul domain, shared, pjap-mock
  - `~/aksatax-fe` (branch dasar `development`): aplikasi web Next.js + salinan `packages/shared`
  - `~/odoo-coretax-integration` (`coretax_integration`, Odoo 19): modul Odoo, versi naik ke `19.0.1.2.0`

## 1. Latar belakang

Halaman Apps Store modul Odoo mencantumkan batasan yang hanya bisa dikerjakan di aplikasi AksaTax karena
Public API AksaTax belum menyediakannya:

1. mengubah Lawan Transaksi yang sudah ada;
2. membatalkan atau mengganti e-Faktur, nota retur, dan unduh PDF faktur pajak;
3. penandatanganan dan pengiriman Bukti Potong ke DJP;
4. Faktur Masukan dan SPT;
5. kode transaksi 07/08 dan 10.

Paket ini menyelesaikan butir 1, 2, dan 5. Butir 3 dan 4 adalah sub-proyek berikutnya.

Hasil pemeriksaan kode (2026-10-29):

| Fitur | Status di AksaTax | Kerja yang dibutuhkan |
|---|---|---|
| Ubah lawan transaksi | `CounterpartiesService.update` + `counterpartyUpdateSchema` ada | Endpoint publik |
| Batalkan e-Faktur | `FakturKeluaranService.requestCancel(actor, id, alasan)` ada | Endpoint publik |
| PDF faktur pajak | `FakturKeluaranService.pdf(actor, id)` ada (APPROVED/CANCEL_REQUESTED/CANCELLED) | Endpoint publik |
| Kode 07/08 | `fakturKeluaranSchema` sudah menerima `idKeteranganTambahan` (wajib untuk 07/08) | Hanya Odoo |
| Faktur pengganti | `fakturKeluaranSchema` sudah menerima `fgPengganti` + `nomorFakturDiganti` | Hanya Odoo |
| Kode 10 | Tidak ada di `KD_JENIS_TRANSAKSI` | Shared (dua repo) + Odoo |
| Nota retur | Provider PJAP punya `uploadReturPk/Pm`, `listReturPk/Pm`, `batalReturPk/Pm`; belum ada modul domain | Modul domain + web + Odoo |

## 2. Tujuan dan kriteria sukses

Dari Odoo, tanpa membuka aplikasi AksaTax, pengguna dapat:

- memperbarui data lawan transaksi yang sudah tertaut ke AksaTax;
- mengirim e-Faktur kode 07 dan 08 (dengan keterangan tambahan) serta kode 10;
- membatalkan e-Faktur yang sudah disetujui DJP, dan statusnya kembali menjadi "Dibatalkan";
- membuat faktur pengganti untuk e-Faktur yang sudah disetujui; e-Faktur lama berstatus "Diganti";
- mengunduh PDF faktur pajak resmi dan menyimpannya sebagai lampiran faktur Odoo;
- mengirim nota retur pajak keluaran (dari nota kredit pelanggan) dan pajak masukan (dari nota kredit
  vendor), serta membatalkannya, dengan status kembali otomatis.

Nota retur yang dibuat dari Odoo terlihat dan dapat dikelola di aplikasi web AksaTax.

## 3. Keputusan

| Topik | Keputusan |
|---|---|
| Cakupan repo | AksaTax backend + web + modul Odoo dikerjakan dalam paket ini |
| Nota retur | Sisi keluaran dan masukan; modul domain penuh (tabel, status, job, audit, event, API, web) |
| Kode 10 | Termasuk; diteruskan apa adanya ke PJAP |
| Scope API key | Tidak ada scope baru; memakai `faktur:baca`, `faktur:tulis`, `faktur:kirim` |
| UI AksaTax | Menu dan form minimal untuk nota retur; kode 10 dan keterangan 08 lewat enum |
| Faktur pengganti di Odoo | Tombol "Ganti e-Faktur" membuka kunci faktur; e-Faktur lama berstatus "Diganti" setelah pengganti disetujui |
| PDF di Odoo | Tombol manual; disimpan sebagai satu lampiran faktur yang diperbarui bila diunduh ulang |
| Rilis | Empat fase; AksaTax selalu di-deploy sebelum rilis Odoo yang membutuhkannya |

### Di luar cakupan

- Tanda tangan dan pengiriman bukti potong ke DJP, Faktur Masukan (penarikan dan pengkreditan), SPT.
- Pembuatan nota retur otomatis saat nota kredit diposting (pengiriman selalu lewat tombol).
- Perubahan pada alur e-Faktur yang sudah ada selain yang disebut di dokumen ini.

## 4. AksaTax backend (`aksatax-be`)

### 4.1 Endpoint publik dari service yang sudah ada

Semua di `apps/api/src/integrasi/public-api.controller.ts`, mengikuti pola yang ada
(`@ApiScopeRule`, `svc()`, `ZodPipe`, `IdempotencyInterceptor`), dan didokumentasikan di `openapi.ts`.

| Metode & path | Scope / peran | Service | Respons |
|---|---|---|---|
| `PUT /public/lawan-transaksi/:id` | `faktur:tulis`, `WRITE_ROLES` | `CounterpartiesService.update(a, id, body)` dengan `counterpartyUpdateSchema` | 200 `publicLawan(c)` |
| `POST /public/faktur/:id/batal` | `faktur:kirim`, `SUBMIT_ROLES`, `sends: true` | `FakturKeluaranService.requestCancel(a, id, alasan)` | 202 `{ id, status: "CANCEL_REQUESTED" }` |
| `GET /public/faktur/:id/pdf` | `faktur:baca` | `FakturKeluaranService.pdf(a, id)` | 200 `application/pdf` + `Content-Disposition` |

- Body batal: `{ alasan: string }`, 5–255 karakter (skema `publicBatalSchema` di `public-schemas.ts`).
- Kesalahan memakai `AppError` yang sudah ada: 404 tidak ditemukan, 409 transisi status tidak sah,
  409 `PDF_NOT_AVAILABLE`, 409 `COUNTERPARTY_EXISTS` (NPWP bentrok dengan lawan transaksi lain).
- PDF memakai `@Res()` seperti `faktur.module.ts:172-178`, nama file dibersihkan dengan regex yang sama.

### 4.2 Kode transaksi 10 dan keterangan tambahan

Di `packages/shared/src/enums.ts` (dan salinan identiknya di `aksatax-fe`, lihat 5.1):

- `KD_JENIS_TRANSAKSI['10'] = 'Penyerahan lainnya'`.
- `ID_KETERANGAN_TAMBAHAN_07` ditambah `'30'` (PPN DTP bekal khusus operasi tertentu TA 2025) dan `'31'`
  (rumah tapak dan satuan rumah susun DTP TA 2026).
- `ID_KETERANGAN_TAMBAHAN_08` baru, `'1'`–`'10'`, dengan label sesuai `l10n_id_efaktur_coretax`.
- **[CEK SPEK]** Pastikan adapter PajakExpress (`tax-provider/pajakexpress/pe.adapter.ts`) dan PJAP
  menerima kode 10 dan `idKeteranganTambahan` dua digit tanpa transformasi tambahan. Bila PJAP butuh
  cap fasilitas terpisah (TD.011xx), tambahkan `capFasilitas` opsional ke `fakturKeluaranSchema` dan
  teruskan di adapter; Odoo mengirimkannya dari `l10n_id_coretax_facility_info_07/08`.

### 4.3 Modul domain `nota-retur`

Folder `apps/api/src/nota-retur/` dengan pola modul `wapu` (service, workflow, constants, module).

**Tabel `nota_retur`** (`db/schema-nota-retur.ts`, migrasi `migrations/0017_nota_retur.sql`, RLS per tenant
seperti tabel domain lain):

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid | |
| `sisi` | text | `KELUARAN` / `MASUKAN` |
| `faktur_keluaran_id` | uuid null | Wajib untuk `KELUARAN`, FK ke `faktur_keluaran` |
| `nomor_faktur` | text | Nomor faktur pajak asal (17 digit) |
| `counterparty_id` | uuid null | Pembeli (keluaran, dari faktur) / penjual (masukan) |
| `nama_lawan`, `npwp_lawan`, `alamat_penjual` | text | Salinan untuk tampilan dan payload PM |
| `nomor_dokumen_retur` | text | Unik per tenant + sisi |
| `tanggal_retur` | date | |
| `masa_pajak_retur`, `tahun_pajak_retur` | int | Diturunkan dari `tanggal_retur` |
| `nilai_retur_dpp`, `nilai_retur_ppn`, `nilai_retur_ppnbm` | bigint | Rupiah |
| `tarif_ppn` | numeric | Default dari faktur asal (keluaran) atau 12 |
| `referensi` | text null | Mis. nomor nota kredit Odoo |
| `status` | text | `DRAFT`, `SUBMITTED`, `APPROVED`, `REJECTED`, `CANCEL_REQUESTED`, `CANCELLED` |
| `provider_id` | bigint null | ID retur di PJAP |
| `last_error`, `alasan_batal` | text null | |
| `created_at`, `updated_at` | timestamptz | |

**Service `NotaReturService`**: `create`, `list` (filter sisi, masa, tahun, status), `get`, `submit`,
`requestCancel`, `remove` (hanya `DRAFT`). Validasi:

- `KELUARAN`: faktur keluaran ada, berstatus `APPROVED`, punya `nomorFaktur`; total DPP dan PPN retur
  yang belum dibatalkan ditambah retur baru tidak melebihi `totalDpp`/`totalPpn` faktur.
- `MASUKAN`: `nomorFaktur` 17 digit; `nama_lawan` dan `npwp_lawan` wajib.
- Nilai DPP > 0, PPN ≥ 0, PPnBM ≥ 0; `tanggalRetur` tidak di masa depan.
- Transisi status lewat `assertTransition`; setiap perubahan tercatat di audit.

**Workflow `NotaReturWorkflow`** (worker, idempoten, pola `WapuWorkflow`):

- Job `RETUR_UPLOAD`: `uploadReturPk`/`uploadReturPm` → `ApprovalResult` → `APPROVED`/`REJECTED`
  (atau polling `listReturPk/Pm` bila hasil tertunda).
- Job `RETUR_BATAL`: `batalReturPk`/`batalReturPm(nomorDokumenRetur)` → `CANCELLED`.
- Event saat `APPROVED`/`REJECTED`/`CANCELLED`: `retur.approved`, `retur.rejected`, `retur.cancelled`,
  payload `{ id, nomor: nomorDokumenRetur, status, sisi, masa, tahun }`.

**Event dan status dokumen**: tiga event baru di `DOMAIN_EVENTS` (`packages/shared/src/events.ts`);
`nota-retur` ditambahkan ke `DOC_TYPES` untuk `GET /public/dokumen/:jenis/:id`.

**Endpoint publik**:

| Metode & path | Scope / peran | Respons |
|---|---|---|
| `POST /public/nota-retur` | `faktur:tulis`, `WRITE_ROLES` | 201 `publicRetur` (status `DRAFT`) |
| `GET /public/nota-retur/:id` | `faktur:baca` | 200 `publicRetur` |
| `POST /public/nota-retur/:id/kirim` | `faktur:kirim`, `SUBMIT_ROLES`, `sends` | 202 `{ id, status: "SUBMITTED" }` |
| `POST /public/nota-retur/:id/batal` | `faktur:kirim`, `SUBMIT_ROLES`, `sends` | 202 `{ id, status: "CANCEL_REQUESTED" }` |

Body `POST /public/nota-retur`:

```json
{
  "sisi": "KELUARAN",
  "fakturId": "<uuid faktur keluaran AksaTax, wajib untuk KELUARAN>",
  "nomorFaktur": "<17 digit, wajib untuk MASUKAN>",
  "namaPenjual": "<MASUKAN>", "npwpPenjual": "<MASUKAN>", "alamatPenjual": "<MASUKAN, opsional>",
  "nomorDokumenRetur": "RINV/2026/00003",
  "tanggalRetur": "2026-10-29",
  "nilaiReturDpp": 1000000, "nilaiReturPpn": 110000, "nilaiReturPpnbm": 0,
  "tarifPpn": 12,
  "referensi": "RINV/2026/00003"
}
```

`publicRetur`: `{ id, sisi, status, nomorDokumenRetur, nomorFaktur, tanggalRetur, masaPajakRetur,
tahunPajakRetur, nilaiReturDpp, nilaiReturPpn, nilaiReturPpnbm, referensi, lastError }`.

**Controller internal** `/nota-retur` (untuk web): `GET /`, `GET /:id`, `POST /`, `POST /:id/kirim`,
`POST /:id/batal`, `DELETE /:id`, dan `GET /faktur/:fakturId` (retur milik satu faktur keluaran).

**pjap-mock**: rute upload, list, dan batal retur PK/PM di `apps/pjap-mock/src/routes/`.

## 5. Aplikasi web AksaTax (`aksatax-fe`)

### 5.1 Shared

`packages/shared` di `aksatax-fe` adalah salinan; perubahan 4.2, event `retur.*`, dan tipe DTO nota retur
dibuat identik di kedua repo (diff berkas harus kosong untuk bagian yang disentuh).

### 5.2 Halaman

- `lib/nota-retur.ts`: klien API (`list`, `detail`, `create`, `kirim`, `batal`, `hapus`, `byFaktur`),
  pola `lib/efaktur-ext.ts`.
- `lib/nav.ts`: menu "Nota Retur" di grup e-Faktur.
- `app/(app)/nota-retur/page.tsx`: tab Keluaran | Masukan, filter masa/tahun (`masa-picker`), kolom
  nomor dokumen retur, nomor faktur asal, lawan transaksi, tanggal, DPP, PPN, status (`status-badge`),
  referensi; aksi Kirim (draft), Batalkan (disetujui, dialog alasan), Hapus (draft).
- `app/(app)/nota-retur/[id]/page.tsx`: detail, pesan error DJP, aksi yang sama.
- `app/(app)/nota-retur/baru/page.tsx`: form minimal. Keluaran: pilih faktur keluaran disetujui, nilai
  default sisa faktur. Masukan: nomor faktur pajak penjual, NPWP/nama/alamat penjual, nilai.
- `app/(app)/faktur-keluaran/[id]/page.tsx`: bagian "Nota retur" berisi retur terkait dan tombol
  "Buat nota retur" yang terisi faktur ini.
- Form faktur: kode 10 dan keterangan 08 muncul dari enum; validasi 07/08 yang ada tetap berlaku.

## 6. Modul Odoo (`coretax_integration`)

### 6.1 Klien API (`tools/client.py`)

Metode baru: `update_counterparty(id, payload, key)`, `cancel_faktur(id, alasan, key)`,
`faktur_pdf(id) -> bytes`, `create_retur(payload, key)`, `get_retur(id)`, `send_retur(id, key)`,
`cancel_retur(id, alasan, key)`. `faktur_pdf` memakai mode respons biner baru di `request()` (tanpa
parsing JSON, tetap tercatat di `coretax.log` tanpa isi biner). Respons 404 pada endpoint baru
diterjemahkan menjadi `AksaTaxError` dengan pesan "Fitur ini butuh AksaTax versi terbaru."

### 6.2 Lawan transaksi

- `res.partner.action_coretax_update_counterparty()`: untuk kontak yang sudah cocok, kirim
  `PUT lawan-transaksi/:id` dengan `_coretax_counterparty_payload(connection)`.
- Tombol "Perbarui di AksaTax" di form kontak (bagian Coretax) dan aksi server untuk banyak kontak di
  daftar Lawan Transaksi. Hasil per kontak dicatat di chatter; error ditampilkan dengan pesan dari AksaTax.

### 6.3 Kode transaksi 07/08/10

- `SUPPORTED_TRX_CODES` menjadi `01`–`09` di Fase 1 (blokir 07/08 di `_coretax_check` dihapus) dan
  ditambah `10` di Fase 2.
- `_coretax_payload`: untuk 07/08 isi `idKeteranganTambahan` = angka dua digit terakhir dari
  `l10n_id_coretax_add_info_07/08` tanpa nol di depan (`TD.00505` → `"5"`, `TD.00530` → `"30"`).
- `_coretax_check`: error "Pilih keterangan tambahan untuk kode 07/08 (tab Info Lainnya)" bila kosong.
- Bila 4.2 menambahkan `capFasilitas`, isi dari `l10n_id_coretax_facility_info_07/08`.

### 6.4 Batalkan e-Faktur

- `coretax.faktur.action_open_cancel_wizard()` → wizard `coretax.faktur.cancel.wizard` (alasan wajib).
- Konfirmasi memanggil `cancel_faktur`; state `cancel_requested`; event `faktur.cancelled` (penangan
  yang ada) menjadikan `cancelled`. Tombol juga tersedia di header faktur Odoo saat
  `coretax_state == 'approved'`.
- Pengaman reset/cancel faktur yang ada tetap berlaku; setelah `cancelled` faktur bebas di-reset.

### 6.5 Faktur pengganti

- State baru di `coretax.faktur`: `replaced` ("Diganti"); tidak termasuk `LOCKING_STATES`.
- Field baru: `replace_requested` (Boolean), `replaced_by_id` (Many2one `coretax.faktur`).
- `action_request_replacement()` pada e-Faktur `approved`: set `replace_requested`, catat di chatter.
  `action_cancel_replacement()` menghapus tanda selama belum ada e-Faktur pengganti.
- `_coretax_guard` mengizinkan reset faktur bila e-Faktur terakhirnya `approved` dan `replace_requested`.
- `_coretax_create_faktur`: bila e-Faktur sebelumnya `approved` + `replace_requested`, payload berisi
  `fgPengganti: "1"` dan `nomorFakturDiganti` = nomor e-Faktur sebelumnya.
- Saat e-Faktur pengganti menjadi `approved`, e-Faktur lama menjadi `replaced` dan `replaced_by_id`
  diisi.

### 6.6 PDF faktur pajak

- `coretax.faktur.action_download_pdf()` untuk state `approved`, `cancel_requested`, `cancelled`,
  `replaced`: unduh PDF, buat atau perbarui satu `ir.attachment` "Faktur Pajak <nomor>.pdf" pada faktur
  Odoo, lalu kembalikan aksi unduh. Tombol juga di header faktur Odoo.

### 6.7 Nota retur

**Model `coretax.retur`** (`mail.thread`):

| Field | Keterangan |
|---|---|
| `sisi` | `keluaran` / `masukan` |
| `move_id` | Nota kredit (`out_refund` / `in_refund`), wajib |
| `origin_move_id` | `move_id.reversed_entry_id` |
| `faktur_id` | e-Faktur asal (keluaran) |
| `nomor_faktur` | Nomor faktur pajak asal |
| `partner_id` | Pembeli / penjual |
| `nomor_dokumen_retur` | Default nama nota kredit |
| `tanggal_retur`, `masa`, `tahun` | Default tanggal nota kredit |
| `nilai_dpp`, `nilai_ppn`, `nilai_ppnbm`, `tarif_ppn` | Dari baris pajak PPN nota kredit (mata uang perusahaan) |
| `state` | `new`, `draft`, `submitted`, `approved`, `rejected`, `cancel_requested`, `cancelled`, `error` |
| `aksatax_id`, `last_error` | |

**Field tagihan vendor**: `account.move.coretax_vendor_tax_number` ("Nomor Faktur Pajak vendor",
17 digit, hanya `in_invoice`), di tab Info Lainnya.

**Alur**:

- Tombol "Kirim nota retur" di nota kredit terposting membuka wizard `coretax.retur.wizard` berisi nilai
  terhitung (bisa diubah) dan nomor dokumen.
  - Keluaran: syarat faktur asal punya e-Faktur `approved`; payload memakai `fakturId` = `aksatax_id`
    e-Faktur asal.
  - Masukan: syarat tagihan asal punya `coretax_vendor_tax_number`; payload memakai `nomorFaktur`,
    nama, NPWP, dan alamat vendor.
- Konfirmasi: `create_retur` lalu `send_retur`; kegagalan menjadikan `error` dengan pesan yang bisa dibaca.
- Event `retur.approved/rejected/cancelled` ditambahkan ke daftar event yang diambil dan diterapkan ke
  `coretax.retur` lewat `aksatax_id`.
- Tombol "Batalkan nota retur" (wizard alasan) untuk retur `approved`.
- Nota kredit tidak bisa di-reset, dibatalkan, atau dihapus selama returnya punya `aksatax_id` dan
  state bukan `rejected`/`cancelled`/`error`.
- Smart button "Nota Retur" di nota kredit; menu Coretax › e-Faktur › Nota Retur.
- Hak akses mengikuti `coretax.faktur` (user baca/tulis/buat, manager penuh).

### 6.8 Halaman Apps Store dan README

Daftar "Batasan saat ini" diperbarui: tersisa tanda tangan/pengiriman bukti potong, Faktur Masukan, dan
SPT. Bagian e-Faktur menyebut batal, pengganti, PDF, kode 07/08/10, dan nota retur.

## 7. Tahapan rilis

| Fase | AksaTax | Odoo |
|---|---|---|
| 1. Buka yang sudah ada | 4.1 | 6.1 (sebagian), 6.2, 6.3 (07/08), 6.4, 6.5, 6.6 |
| 2. Kode 10 | 4.2 + 5.1 (enum) | 6.3 (10) |
| 3. Nota retur | 4.3 + 5.1 + 5.2 | 6.1 (retur), 6.7 |
| 4. Dokumen | | 6.8 |

Setiap fase bisa dirilis sendiri dan mendapat rencana implementasi sendiri. Odoo yang lebih baru tetap
jalan di AksaTax yang lebih lama (6.1, 404).
Branch fitur dibuat dari `development` di kedua repo AksaTax; di Odoo dari `main`, lalu di-merge ke
`main` dan `19.0`.

## 8. Pengujian

- `aksatax-be`: vitest untuk setiap endpoint publik baru (gaya `public-lawan.test.ts`: service di-mock,
  scope/peran, bentuk respons, tidak ada field internal yang bocor); test service dan workflow nota retur
  dengan provider palsu (validasi nilai, transisi, upload → disetujui/ditolak, batal, idempotensi job);
  test skema shared (kode 10, `ID_KETERANGAN_TAMBAHAN_08`); `pnpm typecheck` dan `pnpm test`.
- `aksatax-fe`: `pnpm typecheck` dan `next build`.
- Odoo: test per fitur dengan `FakeAksaTax` (payload 07/08/10, batal + event, pengganti sampai `replaced`,
  PDF menjadi lampiran tunggal, retur keluaran/masukan + event + pengaman nota kredit, 404 versi lama),
  suite penuh, dan instal bersih.

## 9. Risiko dan hal yang perlu diverifikasi

- **[CEK SPEK] Kode 10 dan cap fasilitas**: diverifikasi ke PJAP sebelum Fase 2 dirilis ke produksi.
- **Aturan retur DJP**: siapa yang boleh membuat retur pajak keluaran (pembeli non-PKP) dan batas nilai
  retur; validasi 4.3 memakai batas total faktur, PJAP tetap menjadi penentu akhir.
- **Faktur pengganti**: hanya untuk faktur `APPROVED`; pembeli dan masa harus sesuai aturan DJP. Penolakan
  DJP tampil sebagai `rejected` pada e-Faktur pengganti; e-Faktur lama tetap `approved`.
- **Migrasi `0017_nota_retur.sql`**: diuji di DB lokal, bukan produksi.
- **Duplikasi `packages/shared`**: perubahan harus identik di dua repo; diff dicek di setiap fase.
