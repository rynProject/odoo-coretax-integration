# Paket e-Faktur Fase 1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dari Odoo, pengguna bisa memperbarui lawan transaksi, mengirim e-Faktur kode 07/08, membatalkan e-Faktur, membuat faktur pengganti, dan mengunduh PDF faktur pajak, lewat tiga endpoint Public API AksaTax baru.

**Architecture:** AksaTax (`~/aksatax-be`) membuka tiga service domain yang sudah ada di `PublicApiController` (pola `svc()` + `@ApiScopeRule`). Modul Odoo menambah metode klien, tombol/wizard, dan logika versi e-Faktur untuk pengganti. Kode 07/08 dan faktur pengganti sudah diterima `POST /public/faktur`, jadi hanya Odoo yang berubah untuk keduanya.

**Tech Stack:** AksaTax: NestJS + zod + drizzle, vitest, pnpm. Odoo 19 (`account`, `l10n_id_efaktur_coretax`), test `AccountTestInvoicingCommon` + `FakeAksaTax`.

**Spec:** `docs/superpowers/specs/2026-10-29-efaktur-retur-lawan-design.md` (Fase 1 = bagian 4.1, 6.1–6.6)

## Global Constraints

- Tidak ada scope API key baru: `faktur:baca`, `faktur:tulis`, `faktur:kirim`.
- Pesan untuk pengguna dalam Bahasa Indonesia, kalimat pendek, menyebut langkah perbaikan.
- Respons 404 tanpa objek `error` dari AksaTax berarti rute belum ada → `AksaTaxError` kode `NOT_SUPPORTED`, pesan "Fitur ini butuh AksaTax versi terbaru. Minta admin AksaTax memperbarui server."
- `SUPPORTED_TRX_CODES` di Fase 1 = `01`–`09`; kode 10 tetap ditolak sampai Fase 2.
- Versi modul Odoo `19.0.1.2.0`.
- Branch: `~/aksatax-be` → `feat/efaktur-fase1` dari `development`; Odoo → `feat/efaktur-fase1` dari `main`.
- `git add` hanya path eksplisit. Pesan commit AksaTax memakai gaya repo (`[IMP] …`); Odoo memakai `feat(efaktur): …`.

**Perintah test AksaTax**

```bash
cd ~/aksatax-be/apps/api && pnpm exec vitest run src/integrasi/          # fokus: tambahkan nama file
cd ~/aksatax-be && pnpm --filter @aksatax/api typecheck
```

Baseline: 38 test di `src/integrasi/` lulus, typecheck bersih.

**Perintah test Odoo** (root repo = addons path, modul di `coretax_integration/`)

```bash
cd ~/odoo && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python odoo-bin --addons-path=/home/odoo/odoo/addons,/home/odoo/odoo-coretax-integration \
  -d coretax_pph_test -u coretax_integration --test-enable --test-tags "$TAGS" --stop-after-init --http-port=8899 --log-level=test 2>&1 \
  | grep -E "FAIL|ERROR|Traceback|tests when loading"
```

`TAGS` contoh: `/coretax_integration:TestEfakturFase1`. Baseline: 66 test lulus. Lulus = `0 failed, 0 error(s)`.

## Review Focus

1. Batal dari status selain `approved` (mis. `cancel_requested` diklik dua kali) → Odoo menolak sebelum memanggil AksaTax → test `test_cancel_only_from_approved` (Task 6).
2. PDF diminta sebelum disetujui DJP → pesan AksaTax "PDF resmi tersedia setelah faktur disetujui DJP" sampai ke pengguna, bukan galat mentah → test `test_pdf_not_available_message` (Task 3).
3. Kontak belum cocok dengan AksaTax diperbarui → tidak ada panggilan API, pesan minta cocokkan dulu → test `test_update_unmatched_partner` (Task 4).
4. Penggantian dibuka lalu faktur dibatalkan (`button_cancel`) → tetap diblokir, karena e-Faktur masih sah di DJP → test `test_cancel_blocked_while_replacing` (Task 7).
5. e-Faktur pengganti ditolak DJP lalu dikirim ulang → kiriman ulang tetap membawa `fgPengganti` dan nomor lama → test `test_replacement_rejected_then_resent` (Task 7).

---

### Task 1: AksaTax `PUT /public/lawan-transaksi/:id`

**Files:**
- Modify: `~/aksatax-be/apps/api/src/integrasi/public-api.controller.ts`
- Modify: `~/aksatax-be/apps/api/src/integrasi/openapi.ts` (path `/lawan-transaksi/{id}`)
- Test: `~/aksatax-be/apps/api/src/integrasi/public-lawan.test.ts`

**Interfaces:**
- Produces: `PublicApiController.updateLawan(a: ApiActor, id: string, body: CounterpartyInput)` → `publicLawan(c)`; rute `PUT /public/lawan-transaksi/:id`.

- [ ] **Step 1: Buat branch** `cd ~/aksatax-be && git checkout -b feat/efaktur-fase1 development`

- [ ] **Step 2: Tulis test yang gagal** (describe baru di `public-lawan.test.ts`, pakai `actor`, `body`, `created` yang ada):

```ts
describe('PUT /public/lawan-transaksi/:id', () => {
  it('memperbarui lewat CounterpartiesService.update dan hanya mengembalikan field publik', async () => {
    const update = vi.fn().mockResolvedValue({ ...created, nama: 'PT Sumber Makmur Sentosa Tbk' });
    const controller = new PublicApiController({} as never, {} as never);
    const svc = vi.spyOn(controller as unknown as { svc: (t: string) => unknown }, 'svc').mockReturnValue({ update });
    const res = await controller.updateLawan(actor, 'cp-1', body);
    expect(svc).toHaveBeenCalledWith('counterparties');
    expect(update).toHaveBeenCalledWith(actor, 'cp-1', body);
    expect(res).toEqual({ id: 'cp-1', jenisId: 'NPWP', npwp: '0013456785073000', nama: 'PT Sumber Makmur Sentosa Tbk', vswpStatus: null });
  });

  it('hanya untuk kunci ber-izin faktur:tulis milik peran penulis', () => {
    expect(Reflect.getMetadata('aksatax:api-scope', PublicApiController.prototype.updateLawan))
      .toEqual({ scope: 'faktur:tulis', roles: WRITE_ROLES });
  });

  it('terdokumentasi di OpenAPI', () => {
    expect(buildOpenApi().paths['/lawan-transaksi/{id}'].put).toBeDefined();
  });
});
```

- [ ] **Step 3: Jalankan, pastikan gagal** `pnpm exec vitest run src/integrasi/public-lawan.test.ts` → `updateLawan is not a function`.

- [ ] **Step 4: Implementasi.** `@ApiScopeRule({ scope: 'faktur:tulis', roles: WRITE_ROLES })` + `@Put('lawan-transaksi/:id')`, `ParseUUIDPipe` untuk id, `ZodPipe(counterpartyUpdateSchema)` (impor dari `@aksatax/shared`), panggil `svc<CounterpartiesService>('counterparties').update(a, id, body)`, kembalikan `publicLawan`. OpenAPI: `put: op('Perbarui lawan transaksi', 'faktur:tulis', { parameters: [idParam], requestBody: json(ref('LawanTransaksiInput')), responses: { 200: … ref('LawanTransaksi') } }, [400, 401, 403, 404, 409, 422, 429])`.

- [ ] **Step 5: Jalankan test integrasi dan typecheck** → semua lulus (41 test), typecheck bersih.

- [ ] **Step 6: Commit** `git add apps/api/src/integrasi/public-api.controller.ts apps/api/src/integrasi/openapi.ts apps/api/src/integrasi/public-lawan.test.ts && git commit -m "[IMP] public API: perbarui lawan transaksi"`

---

### Task 2: AksaTax `POST /public/faktur/:id/batal` dan `GET /public/faktur/:id/pdf`

**Files:**
- Modify: `~/aksatax-be/apps/api/src/integrasi/public-schemas.ts` (`publicBatalSchema`)
- Modify: `~/aksatax-be/apps/api/src/integrasi/public-api.controller.ts`
- Modify: `~/aksatax-be/apps/api/src/integrasi/openapi.ts`
- Create: `~/aksatax-be/apps/api/src/integrasi/public-faktur.test.ts`

**Interfaces:**
- Produces: `publicBatalSchema = z.object({ alasan: z.string().trim().min(5).max(255) })`; `batalFaktur(a, id, body: { alasan: string })` → `{ id, status: 'CANCEL_REQUESTED' }` (HTTP 202); `pdfFaktur(a, id, res: Response)` → kirim PDF.

- [ ] **Step 1: Tulis test yang gagal** di `public-faktur.test.ts` (pola actor/controller/svc spy sama dengan Task 1):
  - `batalFaktur` memanggil `requestCancel(actor, 'f-1', 'Salah harga satuan')` dan mengembalikan `{ id: 'f-1', status: 'CANCEL_REQUESTED' }`; metadata scope `{ scope: 'faktur:kirim', roles: SUBMIT_ROLES, sends: true }`; `Reflect.getMetadata('__httpCode__', PublicApiController.prototype.batalFaktur) === 202`.
  - `publicBatalSchema.safeParse({ alasan: 'abc' }).success === false`; `publicBatalSchema.parse({ alasan: '  Salah harga satuan ' }).alasan === 'Salah harga satuan'`.
  - `pdfFaktur`: `pdf` di-mock mengembalikan `{ pdf: Buffer.from('%PDF-1.7'), filename: 'Faktur 010.000/26.pdf' }`; `res` palsu (`status`, `setHeader` berantai, `send`) menerima `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="Faktur_010.000_26.pdf"`, dan buffer yang sama; scope `{ scope: 'faktur:baca' }`.
  - OpenAPI punya `/faktur/{id}/batal` (post) dan `/faktur/{id}/pdf` (get).

- [ ] **Step 2: Jalankan, pastikan gagal** `pnpm exec vitest run src/integrasi/public-faktur.test.ts`.

- [ ] **Step 3: Implementasi.**
  - `batalFaktur`: `@ApiScopeRule({ scope: 'faktur:kirim', roles: SUBMIT_ROLES, sends: true })`, `@Post('faktur/:id/batal')`, `@HttpCode(202)`, body `ZodPipe(publicBatalSchema)`, `svc<FakturKeluaranService>('faktur').requestCancel(a, id, body.alasan)`.
  - `pdfFaktur`: `@ApiScopeRule({ scope: 'faktur:baca' })`, `@Get('faktur/:id/pdf')`, `@Res()`; isi sama dengan `faktur.module.ts:172-178` (header + `send`), nama file dibersihkan `/[^\w.-]/g` → `_`.
  - OpenAPI: `batal` (202, kesalahan [400, 401, 403, 404, 409, 422, 429], deskripsi: hasil lewat event `faktur.cancelled`); `pdf` (200 `application/pdf` binary, 409 `PDF_NOT_AVAILABLE`).

- [ ] **Step 4: Jalankan test integrasi dan typecheck** → lulus, bersih.

- [ ] **Step 5: Commit** `git add apps/api/src/integrasi/public-schemas.ts apps/api/src/integrasi/public-api.controller.ts apps/api/src/integrasi/openapi.ts apps/api/src/integrasi/public-faktur.test.ts && git commit -m "[IMP] public API: batalkan faktur dan unduh PDF faktur pajak"`

---

### Task 3: Odoo — metode klien baru, mode biner, `NOT_SUPPORTED`, versi 19.0.1.2.0

**Files:**
- Modify: `coretax_integration/tools/client.py`
- Modify: `coretax_integration/tests/common.py` (`FakeResponse`, `FakeAksaTax`)
- Modify: `coretax_integration/__manifest__.py` (versi `19.0.1.2.0`)
- Create: `coretax_integration/tests/test_efaktur_fase1.py` (kelas `TestEfakturFase1(CoretaxTestCommon)`, tag sama dengan `TestCoretaxFlow`); daftarkan di `tests/__init__.py`

**Interfaces:**
- Produces (di `AksaTaxClient`):
  - `request(method, path, params=None, payload=None, idempotency_key=None, operation="", raw=False)`: bila `raw=True` dan status < 400, kembalikan `response.content` (bytes) tanpa parsing JSON; `Accept: application/pdf, application/json`; dicatat di log tanpa isi biner.
  - `update_counterparty(counterparty_id, payload) -> dict` (`PUT /lawan-transaksi/{id}`).
  - `cancel_faktur(faktur_id, alasan, idempotency_key) -> dict` (`POST /faktur/{id}/batal`, payload `{"alasan": alasan}`).
  - `faktur_pdf(faktur_id) -> bytes` (`GET /faktur/{id}/pdf`, `raw=True`).
- Produces (di `tests/common.py`): `FakeResponse(status, body=None, headers=None, raw=None)` — bila `raw` diisi, `content = raw` dan `json()` melempar `ValueError`; `FakeAksaTax` mengembalikan jawaban rute apa adanya bila jawaban itu sudah `FakeResponse`.

- [ ] **Step 1: Buat branch** `cd /home/odoo/odoo-coretax-integration && git checkout -b feat/efaktur-fase1 main`

- [ ] **Step 2: Tulis test yang gagal** di `test_efaktur_fase1.py`:

```python
def test_faktur_pdf_returns_bytes(self):
    fake, patcher = self._patch({("GET", f"/faktur/{FAKTUR_ID}/pdf"): FakeResponse(
        200, raw=b"%PDF-1.7 contoh", headers={"Content-Type": "application/pdf"})})
    with patcher:
        content = self.connection._client(self.connection).faktur_pdf(FAKTUR_ID)
    self.assertEqual(content, b"%PDF-1.7 contoh")

def test_pdf_not_available_message(self):
    fake, patcher = self._patch({("GET", f"/faktur/{FAKTUR_ID}/pdf"): (409, {"error": {
        "code": "PDF_NOT_AVAILABLE", "message": "PDF resmi tersedia setelah faktur disetujui DJP"}})})
    with patcher, self.assertRaisesRegex(AksaTaxError, "setelah faktur disetujui DJP"):
        self.connection._client(self.connection).faktur_pdf(FAKTUR_ID)

def test_route_missing_means_old_aksatax(self):
    fake, patcher = self._patch({})   # rute tak dikenal → 404 tanpa objek "error"
    with patcher, self.assertRaises(AksaTaxError) as ctx:
        self.connection._client(self.connection).cancel_faktur(FAKTUR_ID, "Salah harga satuan", "k-1")
    self.assertEqual(ctx.exception.code, "NOT_SUPPORTED")
    self.assertIn("AksaTax versi terbaru", ctx.exception.full_message())
```

`FakeAksaTax` saat ini menjawab rute tak dikenal dengan `{"error": {"code": "NOT_FOUND", ...}}`; ubah jawaban default menjadi `FakeResponse(404, {"statusCode": 404, "message": "Cannot … "})` (bentuk NestJS) dan pastikan test lama yang mengandalkan 404 tetap lulus.

- [ ] **Step 3: Jalankan, pastikan gagal** `TAGS=/coretax_integration:TestEfakturFase1`.

- [ ] **Step 4: Implementasi** sesuai Interfaces. Di `_error_from_response`: `status == 404` dan `err` kosong → kode `NOT_SUPPORTED` (tambahkan pesannya ke `FRIENDLY_ERRORS`). Naikkan versi manifest ke `19.0.1.2.0`.

- [ ] **Step 5: Jalankan suite penuh** `TAGS=/coretax_integration` → 69 test, 0 gagal.

- [ ] **Step 6: Commit** `git add coretax_integration/tools/client.py coretax_integration/tests/common.py coretax_integration/tests/__init__.py coretax_integration/tests/test_efaktur_fase1.py coretax_integration/__manifest__.py && git commit -m "feat(efaktur): klien AksaTax untuk perbarui lawan transaksi, batal, dan PDF"`

---

### Task 4: Odoo — Perbarui lawan transaksi di AksaTax

**Files:**
- Modify: `coretax_integration/models/res_partner.py`
- Modify: `coretax_integration/views/res_partner_views.xml` (tombol form + tombol header daftar Lawan Transaksi)
- Test: `coretax_integration/tests/test_efaktur_fase1.py`

**Interfaces:**
- Consumes: `AksaTaxClient.update_counterparty` (Task 3), `_coretax_counterparty_payload(connection)`.
- Produces: `res.partner.action_coretax_update_counterparty()` (multi-record).

- [ ] **Step 1: Tulis test yang gagal**
  - `test_update_counterparty_sends_put`: `partner_a` dengan `coretax_counterparty_id = CP_ID`; rute `("PUT", f"/lawan-transaksi/{CP_ID}")` → 200 `{"id": CP_ID, "jenisId": "NPWP", "npwp": "1234567890123457", "nama": "PT Pelanggan Baru", "vswpStatus": "AKTIF"}`. Assert payload terkirim sama dengan `_coretax_counterparty_payload(self.connection)` dan chatter kontak memuat "diperbarui di AksaTax".
  - `test_update_unmatched_partner`: kontak tanpa `coretax_counterparty_id` → `UserError` berisi "Cocokkan" dan `fake.calls == []`.
  - `test_update_counterparty_error_is_readable`: rute PUT → 409 `{"error": {"code": "COUNTERPARTY_EXISTS", "message": "NPWP sudah dipakai lawan transaksi lain"}}` → `UserError` berisi pesan itu.

- [ ] **Step 2: Jalankan, pastikan gagal.**

- [ ] **Step 3: Implementasi.** Per kontak: tanpa tautan → kumpulkan sebagai masalah "Cocokkan dulu kontak %s dengan AksaTax."; dengan tautan → `client.update_counterparty(...)`, `message_post`. Satu kontak: galat langsung `UserError`. Banyak kontak: ringkasan sukses/gagal lewat notifikasi `display_notification` (pola `action_coretax_match_all`). Tombol "Perbarui di AksaTax" tampil bila `coretax_match_state == 'matched'`.

- [ ] **Step 4: Jalankan suite penuh** → 72 test, 0 gagal.

- [ ] **Step 5: Commit** `git add coretax_integration/models/res_partner.py coretax_integration/views/res_partner_views.xml coretax_integration/tests/test_efaktur_fase1.py && git commit -m "feat(efaktur): perbarui lawan transaksi di AksaTax dari kontak"`

---

### Task 5: Odoo — Kode transaksi 07/08

**Files:**
- Modify: `coretax_integration/models/account_move.py`
- Test: `coretax_integration/tests/test_efaktur_fase1.py`

**Interfaces:**
- Produces: `SUPPORTED_TRX_CODES = {"01", …, "09"}`; `account.move._coretax_keterangan_tambahan() -> str` (`""` bila bukan 07/08).

- [ ] **Step 1: Tulis test yang gagal**
  - `test_code_07_sends_keterangan`: faktur kode `07`, `l10n_id_coretax_add_info_07 = "TD.00505"` → `_coretax_payload(CP_ID)["idKeteranganTambahan"] == "5"`, `kdJenisTransaksi == "07"`, dan `_coretax_check()` tanpa error.
  - `test_code_07_two_digit_keterangan`: `TD.00530` → `"30"`.
  - `test_code_08_sends_keterangan`: kode `08`, `l10n_id_coretax_add_info_08 = "TD.00510"` → `"10"`.
  - `test_code_07_requires_keterangan`: kode `07` tanpa keterangan → error di `_coretax_check()` berisi "keterangan tambahan".
  - `test_code_10_still_unsupported`: kode `10` → error "belum didukung".
  - Payload kode `01` tidak memuat `idKeteranganTambahan`.

- [ ] **Step 2: Jalankan, pastikan gagal.**

- [ ] **Step 3: Implementasi.** `_coretax_keterangan_tambahan`: ambil `l10n_id_coretax_add_info_07/08` sesuai kode, kembalikan `str(int(value[-2:]))`. `_coretax_check`: hapus blok "07/08 belum bisa dikirim"; untuk 07/08 tanpa keterangan → error "Pilih keterangan tambahan untuk kode transaksi %s (tab Info Lainnya)."; kode di luar `SUPPORTED_TRX_CODES` tetap pesan lama. `_coretax_payload`: tambahkan `idKeteranganTambahan` hanya bila tidak kosong.

- [ ] **Step 4: Jalankan suite penuh** → 0 gagal.

- [ ] **Step 5: Commit** `git add coretax_integration/models/account_move.py coretax_integration/tests/test_efaktur_fase1.py && git commit -m "feat(efaktur): kirim e-Faktur kode 07/08 dengan keterangan tambahan"`

---

### Task 6: Odoo — Batalkan e-Faktur

**Files:**
- Create: `coretax_integration/wizard/coretax_faktur_cancel_wizard.py` (+ impor di `wizard/__init__.py`)
- Modify: `coretax_integration/views/coretax_wizard_views.xml`, `views/coretax_faktur_views.xml`, `views/account_move_views.xml`
- Modify: `coretax_integration/security/ir.model.access.csv` (user: 1,1,1,1)
- Modify: `coretax_integration/models/coretax_faktur.py`, `models/account_move.py`
- Test: `coretax_integration/tests/test_efaktur_fase1.py`

**Interfaces:**
- Consumes: `AksaTaxClient.cancel_faktur` (Task 3).
- Produces: model `coretax.faktur.cancel.wizard` (`faktur_id` Many2one wajib, `alasan` Char wajib, `action_confirm()`); `coretax.faktur.action_open_cancel_wizard()`; `account.move.action_coretax_cancel_efaktur()` (buka wizard untuk `coretax_faktur_id`).

- [ ] **Step 1: Tulis test yang gagal** (siapkan e-Faktur `approved` dengan `aksatax_id = FAKTUR_ID` lewat alur kirim + event approved yang sudah ada di `test_coretax_flow.py`, atau tulis langsung `state`/`aksatax_id`):
  - `test_cancel_sends_alasan`: rute `("POST", f"/faktur/{FAKTUR_ID}/batal")` → 202 `{"id": FAKTUR_ID, "status": "CANCEL_REQUESTED"}`; wizard `alasan = "Salah harga satuan"` → payload `{"alasan": "Salah harga satuan"}`, header `Idempotency-Key` ada, state `cancel_requested`, chatter faktur Odoo memuat alasan.
  - `test_cancelled_event_unlocks_invoice`: setelah itu event `faktur.cancelled` (pola `_process_event` di test lama) → state `cancelled`, `move.button_draft()` tidak melempar.
  - `test_cancel_only_from_approved`: e-Faktur `processing` → `action_open_cancel_wizard()` melempar `UserError`, `fake.calls == []`.
  - `test_cancel_reason_too_short`: alasan `"abc"` → `UserError` berisi "minimal 5 karakter", tanpa panggilan API.

- [ ] **Step 2: Jalankan, pastikan gagal.**

- [ ] **Step 3: Implementasi.** `action_confirm`: cek alasan ≥ 5 karakter dan state `approved`; panggil `cancel_faktur(aksatax_id, alasan, f"{faktur._idempotency_base()}-batal")`; tulis state dari `REMOTE_STATES` (default `cancel_requested`), `last_error=False`; `message_post` di e-Faktur dan faktur Odoo "Pembatalan e-Faktur diajukan ke DJP. Alasan: %s"; `AksaTaxError` → `UserError(full_message)`. Tombol "Batalkan e-Faktur" di header form e-Faktur (`state == 'approved'`) dan faktur Odoo (`coretax_state == 'approved'`), grup `group_coretax_user`.

- [ ] **Step 4: Jalankan suite penuh** → 0 gagal.

- [ ] **Step 5: Commit** (path eksplisit semua file di atas) `git commit -m "feat(efaktur): batalkan e-Faktur dari Odoo dengan alasan"`

---

### Task 7: Odoo — Faktur pengganti

**Files:**
- Modify: `coretax_integration/models/coretax_faktur.py`, `models/account_move.py`
- Modify: `coretax_integration/views/coretax_faktur_views.xml`, `views/account_move_views.xml`
- Test: `coretax_integration/tests/test_efaktur_fase1.py`

**Interfaces:**
- Produces:
  - `STATES` ditambah `("replaced", "Diganti")` (bukan `LOCKING_STATES`, bukan `RECREATE_STATES`).
  - Field `coretax.faktur`: `replace_requested` (Boolean, tracking, copy=False), `replaces_id` (Many2one `coretax.faktur`, "Mengganti e-Faktur"), `replaced_by_id` (Many2one `coretax.faktur`, "Diganti oleh").
  - `coretax.faktur.action_request_replacement()`, `coretax.faktur.action_cancel_replacement()`.
  - `account.move._coretax_replacing() -> coretax.faktur`: e-Faktur faktur ini (termasuk yang diarsipkan) yang `state == 'approved'` dan `replace_requested`, maksimal satu.
  - `account.move.action_coretax_request_replacement()` (delegasi ke `coretax_faktur_id`).

- [ ] **Step 1: Tulis test yang gagal**
  - `test_replacement_flow`: e-Faktur `approved` nomor `0400260000000001` → `action_request_replacement()` → `move.button_draft()` berhasil → ubah `price_unit` → `action_post()` → `coretax_can_send` True → kirim (rute `POST /faktur` menangkap payload) → payload `fgPengganti == "1"`, `nomorFakturDiganti == "0400260000000001"`; e-Faktur baru `replaces_id` = lama. Event `faktur.approved` untuk e-Faktur baru → lama `state == "replaced"`, `replaced_by_id` = baru, `replace_requested` False.
  - `test_cancel_replacement`: buka lalu batalkan penggantian sebelum ada e-Faktur baru → `replace_requested` False, `button_draft()` kembali diblokir.
  - `test_cancel_blocked_while_replacing`: penggantian dibuka → `move.button_cancel()` tetap `UserError`.
  - `test_replacement_rejected_then_resent`: e-Faktur pengganti ditolak (event `faktur.rejected`) → lama tetap `approved`; `action_coretax_send` lagi → payload kiriman ulang tetap `fgPengganti == "1"` dengan nomor lama.
  - `test_request_replacement_only_from_approved`: state `processing` → `UserError`.

- [ ] **Step 2: Jalankan, pastikan gagal.**

- [ ] **Step 3: Implementasi.**
  - `coretax_can_send`, `_coretax_ready_domain`: juga benar bila `_coretax_replacing()` ada (domain: `("coretax_faktur_ids.replace_requested", "=", True)` dengan `active_test` mati sesuai kebutuhan).
  - `_coretax_check`: lewati error "Sudah punya e-Faktur" bila `_coretax_replacing()`.
  - `_coretax_guard(action_label, allow_replacing=False)`: `button_draft` memanggil dengan `allow_replacing=True`; faktur dengan `_coretax_replacing()` dilewati. `button_cancel` tidak berubah.
  - `_coretax_payload`: bila `_coretax_replacing()` → `fgPengganti: "1"`, `nomorFakturDiganti: replacing.nomor_faktur`.
  - `_coretax_create_faktur`: e-Faktur baru mendapat `replaces_id = _coretax_replacing()`.
  - `coretax.faktur.write`: setelah `super()`, bila `vals` menjadikan state `approved` dan `replaces_id` belum `replaced` → `replaces_id.write({"state": "replaced", "replaced_by_id": rec.id, "replace_requested": False})` dan `message_post` di faktur Odoo "e-Faktur %s diganti oleh %s".
  - `action_request_replacement`: hanya `approved` dan belum `replace_requested`; `message_post` "Penggantian e-Faktur dibuka. Reset faktur ke draft, perbaiki, lalu kirim lagi sebagai faktur pengganti." `action_cancel_replacement`: hanya bila belum ada e-Faktur yang `replaces_id`-nya rekaman ini.
  - View: tombol "Ganti e-Faktur" dan "Batalkan penggantian" di form e-Faktur (dan "Ganti e-Faktur" di header faktur Odoo saat `coretax_state == 'approved'`); field `replaces_id`/`replaced_by_id`; alert "e-Faktur ini sudah diganti oleh …" saat `replaced`.

- [ ] **Step 4: Jalankan suite penuh** → 0 gagal.

- [ ] **Step 5: Commit** `git commit -m "feat(efaktur): faktur pengganti dari Odoo, e-Faktur lama berstatus Diganti"` (path eksplisit)

---

### Task 8: Odoo — Unduh PDF faktur pajak sebagai lampiran

**Files:**
- Modify: `coretax_integration/models/coretax_faktur.py`, `models/account_move.py`
- Modify: `coretax_integration/views/coretax_faktur_views.xml`, `views/account_move_views.xml`
- Test: `coretax_integration/tests/test_efaktur_fase1.py`

**Interfaces:**
- Consumes: `AksaTaxClient.faktur_pdf` (Task 3), state `replaced` (Task 7).
- Produces: `PDF_STATES = ("approved", "cancel_requested", "cancelled", "replaced")`; `coretax.faktur.action_download_pdf() -> dict` (`ir.actions.act_url` ke `/web/content/<attachment_id>?download=true`); `account.move.action_coretax_download_pdf()`.

- [ ] **Step 1: Tulis test yang gagal**
  - `test_download_pdf_creates_single_attachment`: e-Faktur `approved` nomor `0400260000000001`; rute PDF (FakeResponse biner) → `action_download_pdf()` dua kali (isi kedua berbeda) → tepat satu `ir.attachment` dengan `res_model == "account.move"`, `res_id == move.id`, `name == "Faktur Pajak 0400260000000001.pdf"`, `mimetype == "application/pdf"`, isi = unduhan terakhir; aksi `url == f"/web/content/{att.id}?download=true"`.
  - `test_pdf_requires_approved`: state `draft` → `UserError` "setelah e-Faktur disetujui DJP", tanpa panggilan API.

- [ ] **Step 2: Jalankan, pastikan gagal.**

- [ ] **Step 3: Implementasi** sesuai Interfaces; cari lampiran lama berdasarkan `res_model`, `res_id`, `name`, lalu `write({"raw": content})` atau `create`. Tombol "Unduh PDF Faktur Pajak" di header form e-Faktur dan faktur Odoo (state di `PDF_STATES`).

- [ ] **Step 4: Jalankan suite penuh, lalu instal bersih** di DB baru `coretax_efaktur_fresh` (`-i coretax_integration`, `TAGS=/coretax_integration`) → 0 gagal; `dropdb coretax_efaktur_fresh`.

- [ ] **Step 5: Commit** `git commit -m "feat(efaktur): unduh PDF faktur pajak sebagai lampiran faktur"` (path eksplisit)
