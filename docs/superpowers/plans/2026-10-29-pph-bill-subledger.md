# PPh di Tagihan Vendor → Subledger Bukti Potong: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PPh yang dipotong di tagihan vendor tercatat di jurnal per kode objek dan otomatis tersambung ke `coretax.bupot` (satu bupot per bill per kode objek).

**Architecture:** PPh = `account.tax` negatif dengan kode objek default. Kode objek per baris bill masuk ke grouping key pajak Odoo, sehingga journal item PPh terpecah per kode objek. `account.move._post` menyinkronkan `coretax.bupot` (buat/update/hapus) dari baris sumber bill dan credit note-nya. Reset/cancel/hapus bill dijaga seperti e-Faktur.

**Tech Stack:** Odoo 19 (`account`, `l10n_id`, `l10n_id_efaktur_coretax`), Python 3, test `AccountTestInvoicingCommon` lewat `odoo-bin --test-enable`.

**Spec:** `docs/superpowers/specs/2026-10-29-pph-bill-subledger-design.md`

## Global Constraints

- Hanya pembelian: `in_invoice`, `in_receipt`, `in_refund`. Tidak ada logika saat payment.
- Pajak PPh = `account.tax` dengan `coretax_tax_object_id` terisi; harus `type_tax_use='purchase'`, `amount_type='percent'`, `amount <= 0`.
- Satu bupot otomatis per (bill, kode objek); bupot `rejected`/`cancelled` tidak aktif.
- Bupot boleh diubah/dihapus oleh sinkronisasi hanya bila state `new` atau `error`.
- Bupot "sudah di AksaTax" = punya `aksatax_id` dan state bukan `rejected`/`cancelled`.
- DPP = Σ `balance` (mata uang perusahaan/IDR) baris produk sumber; PPh GL = −Σ `balance` journal item PPh.
- Teks UI dan pesan error dalam Bahasa Indonesia, gaya sama dengan kode yang ada (`_("...")`, kalimat pendek, menyebut langkah perbaikan).
- `bt_withholding_tax` tidak menjadi dependency; tidak ada perubahan pada alur e-Faktur.
- Versi modul `19.0.1.1.0`.
- Kerjakan di branch `feat/pph-bill-subledger`.

**Menjalankan test.** Odoo butuh folder addons berisi folder bernama `coretax_integration`; root repo ini adalah modulnya:

```bash
ADDONS=$(mktemp -d) && ln -s /home/odoo/odoo-coretax-integration $ADDONS/coretax_integration
# DB uji coretax_pph_test sudah ada (baseline 26 test lulus). Bila belum ada, ganti -u dengan -i.
cd ~/odoo && .venv/bin/python odoo-bin --addons-path=/home/odoo/odoo/addons,$ADDONS \
  -d coretax_pph_test -u coretax_integration --test-enable --test-tags "$TAGS" \
  --stop-after-init --http-port=8899 --log-level=test 2>&1 | grep -E "FAIL|ERROR|Traceback|tests when loading"
```

`TAGS` contoh: `/coretax_integration:TestPphTax` (satu kelas), `/coretax_integration:TestPphTax.test_case_three_lines_gl` (satu test), `/coretax_integration` (semua). Lulus = baris `0 failed, 0 error(s) of N tests`.

## Review Focus

1. Bill valas (USD) → DPP bupot harus dalam IDR dari `balance`, bukan `price_subtotal` → test `test_foreign_currency_bill_dpp_in_idr` (Task 3).
2. Dua baris dengan kode objek sama tetapi tarif PPh berbeda (2% dan SKB 0%) → posting ditolak dengan pesan jelas, bukan satu bupot dengan tarif salah → test `test_mixed_rates_same_object_rejected` (Task 3).
3. Baris dengan analytic berbeda → journal item PPh ikut terpecah per analytic (`use_in_tax_closing=False`), bupot tetap satu dengan PPh GL penuh → test `test_analytic_split_still_one_bupot` (Task 3).
4. Bill diposting ulang setelah bupotnya `rejected` di DJP → bupot baru dibuat tanpa melanggar unique index, yang lama tetap → test `test_repost_after_rejected_creates_new_bupot` (Task 4).
5. Kode objek diganti di baris bill draft tanpa mengubah pajak → journal item PPh harus ikut terpecah ulang → test `test_write_object_on_draft_resyncs_tax_lines` (Task 2).

---

### Task 1: Pajak PPh, template `l10n_id`, dan post-init hook

**Files:**
- Create: `models/account_tax.py`
- Create: `models/template_id.py`
- Modify: `models/__init__.py`
- Modify: `__init__.py`
- Modify: `__manifest__.py` (versi `19.0.1.1.0`, `"post_init_hook": "_coretax_post_init"`)
- Modify: `tests/common.py`
- Create: `tests/test_pph_tax.py`
- Modify: `tests/__init__.py`

**Interfaces:**
- Produces:
  - `account.tax.coretax_tax_object_id` — Many2one `coretax.tax.object`, string "Kode objek PPh (Coretax)". Pajak PPh = field ini terisi.
  - Chart template `id`, xmlid (relatif perusahaan): `l10n_id_tax_group_pph`, `tax_pph23_2`, `tax_pph23_skb`, `tax_pph42_sewa_10`, `tax_pph26_20`.
  - `account.chart.template._get_id_coretax_account_tax_group() -> dict`, `._get_id_coretax_account_tax() -> dict` (dekorator `@template("id", ...)`).
  - `__init__._coretax_post_init(env) -> None`.
  - `tests/common.py`: kelas `PphTestCommon(CoretaxTestCommon)` dengan atribut `pph23`, `pph23_skb`, `pph42`, `ppn` (`tax_PT4`), `obj_teknik` (24-104-01), `obj_konsultan` (24-104-03), `obj_sewa` (28-402-01), dan helper `_bill(lines, post=True, **vals) -> account.move` (`in_invoice`) di mana `lines` = list tuple `(price_unit, taxes)` atau `(price_unit, taxes, tax_object)`; baris memakai `product_a`, quantity 1, partner `partner_a`, `invoice_date="2026-10-01"`. Elemen ketiga (bila ada) mengisi `coretax_tax_object_id` baris (dipakai mulai Task 2).

- [ ] **Step 1: Buat branch**

```bash
git checkout -b feat/pph-bill-subledger
```

- [ ] **Step 2: Tulis test yang gagal** di `tests/test_pph_tax.py` (kelas `TestPphTax(PphTestCommon)`, tag sama dengan `TestCoretaxFlow`):

```python
def test_template_pph_taxes(self):
    expected = [
        (self.pph23, -2.0, "24-104-01", "21210030"),
        (self.pph23_skb, 0.0, "24-104-01", "21210030"),
        (self.pph42, -10.0, "28-402-01", "21210050"),
        (self.env["account.chart.template"].ref("tax_pph26_20"), -20.0, "27-104-01", "21210060"),
    ]
    for tax, amount, code, account in expected:
        self.assertEqual(tax.type_tax_use, "purchase")
        self.assertEqual(tax.amount, amount)
        self.assertEqual(tax.coretax_tax_object_id.code, code)
        self.assertEqual(tax.tax_group_id.name, "PPh")
        reps = tax.invoice_repartition_line_ids | tax.refund_repartition_line_ids
        tax_reps = reps.filtered(lambda r: r.repartition_type == "tax")
        self.assertEqual(set(tax_reps.mapped("account_id.code")), {account})
        self.assertFalse(any(tax_reps.mapped("use_in_tax_closing")))

def test_pph_tax_must_be_negative_purchase_percent(self):
    base = {"name": "PPh salah", "amount_type": "percent", "coretax_tax_object_id": self.obj_teknik.id}
    with self.assertRaises(ValidationError):
        self.env["account.tax"].create({**base, "amount": 2, "type_tax_use": "purchase"})
    with self.assertRaises(ValidationError):
        self.env["account.tax"].create({**base, "amount": -2, "type_tax_use": "sale"})

def test_post_init_hook_restores_missing_taxes(self):
    template = self.env["account.chart.template"]
    template.ref("tax_pph26_20").unlink()
    _coretax_post_init(self.env)
    _coretax_post_init(self.env)
    tax = template.ref("tax_pph26_20")
    self.assertEqual(tax.amount, -20.0)
    self.assertEqual(self.env["account.tax"].search_count(
        [("name", "=", tax.name), ("company_id", "=", self.env.company.id)]), 1)
```

`_coretax_post_init` diimpor dari `odoo.addons.coretax_integration`.

- [ ] **Step 3: Jalankan, pastikan gagal**

Run dengan `TAGS=/coretax_integration:TestPphTax`. Expected: error karena `PphTestCommon` / xmlid `tax_pph23_2` belum ada.

- [ ] **Step 4: Implementasi**

- `models/account_tax.py`: field `coretax_tax_object_id`; `@api.constrains("coretax_tax_object_id", "type_tax_use", "amount_type", "amount")` → `ValidationError` bila PPh tetapi bukan purchase/percent/≤0, pesan mis. "Pajak PPh harus pajak pembelian bertipe persen dengan tarif 0 atau negatif (mis. -2 untuk PPh 23 2%)."
- `models/template_id.py`: tax group `l10n_id_tax_group_pph` (`name` "PPh", `country_id` `base.id`). Empat pajak sesuai tabel spec 4.1, masing-masing:
  - `amount_type` `percent`, `type_tax_use` `purchase`, `tax_group_id` `l10n_id_tax_group_pph`;
  - `invoice_label` "PPh 23" / "PPh 23 SKB" / "PPh 4(2)" / "PPh 26";
  - `coretax_tax_object_id` xmlid penuh `coretax_integration.tax_object_*` (loader chart template me-resolve xmlid yang mengandung titik);
  - `repartition_line_ids` = `Command.create` base + tax untuk `invoice` dan `refund`; baris tax membawa `account_id` (mis. `l10n_id_21210030`) dan `use_in_tax_closing: False`.
- `__init__.py`: `_coretax_post_init(env)` mengikuti pola `~/odoo/addons/l10n_tr_nilvera_einvoice_extended/__init__.py`. Untuk tiap `res.company` dengan `chart_template == "id"` dan tanpa `parent_id`: `Template = env["account.chart.template"].with_company(company)`, susun `{"account.tax.group": ..., "account.tax": ...}` hanya berisi xmlid yang `Template.ref(xmlid, raise_if_not_found=False)` kosong, lalu `Template._load_data(data)`.
- `tests/common.py`: `PphTestCommon` sesuai Interfaces. Ambil pajak lewat `self.env["account.chart.template"].with_company(company).ref("tax_pph23_2")` dst.

- [ ] **Step 5: Jalankan, pastikan lulus**

Run `TAGS=/coretax_integration` (semua). Expected: `0 failed, 0 error(s)` (26 lama + 3 baru).

- [ ] **Step 6: Commit**

```bash
git add __init__.py __manifest__.py models/__init__.py models/account_tax.py models/template_id.py tests/
git commit -m "feat(pph): pajak PPh negatif dengan kode objek default untuk l10n_id"
```

---

### Task 2: Kode objek per baris dan journal item PPh per kode objek

**Files:**
- Create: `models/account_move_line.py`
- Modify: `models/account_tax.py`
- Modify: `models/__init__.py`
- Modify: `tests/test_pph_tax.py`

**Interfaces:**
- Consumes: `account.tax.coretax_tax_object_id`, `PphTestCommon` (Task 1).
- Produces:
  - `account.move.line.coretax_tax_object_id` — Many2one `coretax.tax.object`, `compute="_compute_coretax_tax_object_id"`, `store=True`, `readonly=False`, `copy=True`, `index=True`.
  - Journal item pajak PPh membawa `coretax_tax_object_id` dari baris dasarnya; journal item non-PPh selalu `False`.

- [ ] **Step 1: Tulis test yang gagal** (tambahkan ke `TestPphTax`):

```python
def test_line_object_defaults_from_tax(self):
    bill = self._bill([(1_000_000, self.pph42), (1_000_000, self.ppn)], post=False)
    self.assertEqual(bill.invoice_line_ids.mapped("coretax_tax_object_id"), self.obj_sewa)
    self.assertFalse(bill.invoice_line_ids[1].coretax_tax_object_id)

def test_case_three_lines_gl(self):
    bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23), (20_000_000, self.pph42)])
    pph = bill.line_ids.filtered("tax_line_id").sorted("balance")
    self.assertRecordValues(pph, [
        {"tax_line_id": self.pph42.id, "coretax_tax_object_id": self.obj_sewa.id, "balance": -2_000_000},
        {"tax_line_id": self.pph23.id, "coretax_tax_object_id": self.obj_teknik.id, "balance": -300_000},
    ])
    self.assertEqual(pph.mapped("account_id.code"), ["21210050", "21210030"])
    self.assertEqual(bill.amount_total, 32_700_000)

def test_changed_object_splits_tax_line(self):
    bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23, self.obj_konsultan),
                       (20_000_000, self.pph42)])
    pph23 = bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)
    self.assertEqual(sorted(zip(pph23.mapped("coretax_tax_object_id.code"), pph23.mapped("balance"))),
                     [("24-104-01", -200_000), ("24-104-03", -100_000)])

def test_ppn_not_split(self):
    bill = self._bill([(10_000_000, self.ppn | self.pph23),
                       (5_000_000, self.ppn | self.pph23, self.obj_konsultan)])
    ppn = bill.line_ids.filtered(lambda l: l.tax_line_id == self.ppn)
    self.assertEqual(len(ppn), 1)
    self.assertFalse(ppn.coretax_tax_object_id)
    self.assertAlmostEqual(ppn.balance, 15_000_000 * self.ppn.amount / 100)
    pph = bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)
    self.assertEqual(sum(pph.mapped("balance")), -300_000, "DPP PPh tidak termasuk PPN")

def test_write_object_on_draft_resyncs_tax_lines(self):
    bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23)], post=False)
    self.assertEqual(len(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)), 1)
    bill.invoice_line_ids[1].coretax_tax_object_id = self.obj_konsultan
    self.assertEqual(len(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)), 2)

def test_two_pph_taxes_on_one_line_rejected(self):
    with self.assertRaises(ValidationError):
        self._bill([(1_000_000, self.pph23 | self.pph42)], post=False)
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run `TAGS=/coretax_integration:TestPphTax`. Expected: 6 test baru gagal (field `coretax_tax_object_id` tidak ada di `account.move.line`).

- [ ] **Step 3: Implementasi `account.move.line`** di `models/account_move_line.py`

- `_compute_coretax_tax_object_id` (`@api.depends("tax_ids")`): untuk `display_type == "product"` → kode objek dari pajak PPh pertama di `tax_ids` (atau `False`). Baris lain **harus** di-assign ke nilainya sendiri (`line.coretax_tax_object_id = line.coretax_tax_object_id`), karena journal item pajak mendapat nilai dari grouping key dan tidak boleh terhapus.
- `@api.constrains("tax_ids", "coretax_tax_object_id")` untuk baris produk:
  - lebih dari satu pajak PPh → `ValidationError("Satu baris hanya boleh memakai satu pajak PPh.")`;
  - `coretax_tax_object_id.jenis` ≠ jenis kode objek pajak PPh-nya → `ValidationError`.

- [ ] **Step 4: Implementasi grouping key** di `models/account_tax.py`, pola `~/odoo/addons/hr_expense/models/account_tax.py`:

```python
def _prepare_base_line_for_taxes_computation(self, record, **kwargs):
    results = super()._prepare_base_line_for_taxes_computation(record, **kwargs)
    results["coretax_tax_object_id"] = self._get_base_line_field_value_from_record(
        record, "coretax_tax_object_id", kwargs, self.env["coretax.tax.object"])
    return results

# _prepare_tax_line_for_taxes_computation: sama persis.

def _prepare_base_line_grouping_key(self, base_line):
    # wajib di sini, bukan hanya di repartition key: _sync_tax_lines hanya menghitung
    # ulang pajak bila field di key ini berubah (test_write_object_on_draft_resyncs_tax_lines)
    results = super()._prepare_base_line_grouping_key(base_line)
    results["coretax_tax_object_id"] = base_line["coretax_tax_object_id"].id
    return results

def _prepare_base_line_tax_repartition_grouping_key(self, base_line, base_line_grouping_key, tax_data, tax_rep_data):
    results = super()._prepare_base_line_tax_repartition_grouping_key(
        base_line, base_line_grouping_key, tax_data, tax_rep_data)
    # hanya journal item PPh yang dipecah per kode objek; PPN tetap satu
    results["coretax_tax_object_id"] = (
        base_line["coretax_tax_object_id"].id if tax_data["tax"].coretax_tax_object_id else False)
    return results

def _prepare_tax_line_repartition_grouping_key(self, tax_line):
    results = super()._prepare_tax_line_repartition_grouping_key(tax_line)
    results["coretax_tax_object_id"] = tax_line["coretax_tax_object_id"].id
    return results
```

- [ ] **Step 5: Jalankan semua test, pastikan lulus**

Run `TAGS=/coretax_integration`. Expected: `0 failed, 0 error(s)`. Test e-Faktur lama yang lulus membuktikan faktur penjualan tidak terpengaruh.

- [ ] **Step 6: Commit**

```bash
git add models/ tests/test_pph_tax.py
git commit -m "feat(pph): kode objek per baris bill dan journal item PPh per kode objek"
```

---

### Task 3: Sinkronisasi `coretax.bupot` saat bill diposting

**Files:**
- Modify: `models/coretax_bupot.py`
- Create: `models/account_move_pph.py`
- Modify: `models/__init__.py`
- Create: `tests/test_pph_bupot.py`
- Modify: `tests/__init__.py`

**Interfaces:**
- Consumes: `account.move.line.coretax_tax_object_id` (Task 2), `PphTestCommon` (Task 1).
- Produces (`models/coretax_bupot.py`):
  - Konstanta `EDITABLE_STATES = ("new", "error")`, `INACTIVE_STATES = ("rejected", "cancelled")`.
  - Field: `origin` (Selection `[("bill", "Dari tagihan"), ("manual", "Manual")]`, default `"manual"`, readonly, required); `tarif` (stored compute dari `tax_object_id.tarif`, `readonly=False`); `source_line_ids`, `tax_line_ids` (Many2many `account.move.line`, compute non-stored); `pph_gl`, `pph_selisih` (Monetary compute).
  - `_bill_unique = models.UniqueIndex("(move_id, tax_object_id) WHERE origin = 'bill' AND state NOT IN ('rejected', 'cancelled')", ...)`.
  - `_coretax_is_at_aksatax(self) -> bool`.
- Produces (`models/account_move_pph.py`, `_inherit = "account.move"`):
  - Field `coretax_bupot_ids` (One2many `coretax.bupot`/`move_id`).
  - `_coretax_pph_family(self) -> account.move`: bill + `reversal_move_ids` yang `in_refund` dan posted.
  - `_coretax_pph_groups(self) -> dict`: `{coretax.tax.object: account.move.line}` baris produk ber-pajak PPh dari family, per kode objek.
  - `_coretax_sync_bupot(self) -> None`: untuk bill induk yang posted.
  - Override `_post`.

- [ ] **Step 1: Tulis test yang gagal** di `tests/test_pph_bupot.py` (kelas `TestPphBupot(PphTestCommon)`):

```python
def _three_lines(self, **kw):
    return self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23), (20_000_000, self.pph42)], **kw)

def test_case_three_lines_creates_two_bupots(self):
    bill = self._three_lines()
    bupots = bill.coretax_bupot_ids.sorted(lambda b: b.tax_object_id.code)
    self.assertRecordValues(bupots, [
        {"origin": "bill", "state": "new", "tax_object_id": self.obj_teknik.id, "partner_id": self.partner_a.id,
         "jenis": "BPPU", "jumlah_bruto": 15_000_000, "tarif": 2.0, "pph_gl": 300_000, "pph_estimasi": 300_000,
         "pph_selisih": 0, "fasilitas": "NONE", "dok_jenis": "02", "dok_nomor": bill.name,
         "tanggal_pemotongan": bill.invoice_date, "dok_tanggal": bill.invoice_date},
        {"origin": "bill", "state": "new", "tax_object_id": self.obj_sewa.id, "partner_id": self.partner_a.id,
         "jenis": "BPPU", "jumlah_bruto": 20_000_000, "tarif": 10.0, "pph_gl": 2_000_000, "pph_estimasi": 2_000_000,
         "pph_selisih": 0, "fasilitas": "NONE", "dok_jenis": "02", "dok_nomor": bill.name,
         "tanggal_pemotongan": bill.invoice_date, "dok_tanggal": bill.invoice_date},
    ])
    self.assertEqual(bupots[0].source_line_ids, bill.invoice_line_ids[:2])
    self.assertEqual(bupots[1].source_line_ids, bill.invoice_line_ids[2])
    self.assertEqual(bupots[0].tax_line_ids.tax_line_id, self.pph23)

def test_changed_object_creates_three_bupots(self):
    bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23, self.obj_konsultan),
                       (20_000_000, self.pph42)])
    self.assertEqual(sorted(zip(bill.coretax_bupot_ids.mapped("tax_object_id.code"),
                                bill.coretax_bupot_ids.mapped("pph_gl"))),
                     [("24-104-01", 200_000), ("24-104-03", 100_000), ("28-402-01", 2_000_000)])

def test_skb_bupot_without_journal_item(self):
    bill = self._bill([(10_000_000, self.pph23_skb)])
    self.assertRecordValues(bill.coretax_bupot_ids, [
        {"tax_object_id": self.obj_teknik.id, "jumlah_bruto": 10_000_000, "tarif": 0.0, "pph_gl": 0, "fasilitas": "SKB"}])
    self.assertFalse(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23_skb))

def test_mixed_rates_same_object_rejected(self):
    bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23_skb)], post=False)
    with self.assertRaisesRegex(UserError, "24-104-01"):
        bill.action_post()

def test_foreign_currency_bill_dpp_in_idr(self):
    usd = self.setup_other_currency("USD", rates=[("2026-01-01", 0.0001)])  # 1 USD = 10.000 IDR
    bill = self._bill([(1_000, self.pph23)], currency_id=usd.id)
    self.assertEqual(bill.coretax_bupot_ids.jumlah_bruto, 10_000_000)
    self.assertEqual(bill.coretax_bupot_ids.pph_gl, 200_000)

def test_analytic_split_still_one_bupot(self):
    plan = self.env["account.analytic.plan"].create({"name": "Proyek"})
    a1, a2 = self.env["account.analytic.account"].create([{"name": n, "plan_id": plan.id} for n in ("A1", "A2")])
    bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23)], post=False)
    bill.invoice_line_ids[0].analytic_distribution = {str(a1.id): 100}
    bill.invoice_line_ids[1].analytic_distribution = {str(a2.id): 100}
    bill.action_post()
    self.assertEqual(len(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)), 2)
    self.assertRecordValues(bill.coretax_bupot_ids, [{"jumlah_bruto": 15_000_000, "pph_gl": 300_000}])

def test_auto_bupot_send_payload_and_guards(self):
    bill = self._three_lines()
    bupot = bill.coretax_bupot_ids.filtered(lambda b: b.tax_object_id == self.obj_teknik)
    with self.assertRaises(UserError):
        bupot.unlink()
    self.partner_a.with_company(bill.company_id).coretax_counterparty_id = CP_ID
    fake, patcher = self._patch({
        ("POST", "/bupot-unifikasi"): (201, {"id": "33333333-3333-4333-8333-333333333333", "status": "DRAFT"})})
    with patcher:
        bupot.action_send_draft()
    sent = fake.calls[-1]["json"]
    self.assertEqual((sent["kodeObjek"], sent["jumlahBruto"]), ("24-104-01", 15_000_000))

def test_send_blocked_while_bill_draft(self):
    bill = self._three_lines()
    bill.button_draft()
    with self.assertRaisesRegex(UserError, "belum diposting"):
        bill.coretax_bupot_ids[0].action_send_draft()
```

`test_send_blocked_while_bill_draft` bergantung pada `button_draft` yang mengizinkan reset bila bupot masih `new`; perilaku bawaan Odoo sudah mengizinkannya, Task 4 hanya menambah blokir untuk bupot yang sudah di AksaTax.

- [ ] **Step 2: Jalankan, pastikan gagal**

Run `TAGS=/coretax_integration:TestPphBupot`. Expected: gagal karena `coretax_bupot_ids` tidak ada.

- [ ] **Step 3: Implementasi `coretax.bupot`**

- `origin`, konstanta, dan `UniqueIndex` sesuai Interfaces.
- `tarif`: ubah dari related menjadi `compute="_compute_tarif", store=True, readonly=False` (`rec.tarif = rec.tax_object_id.tarif`). Test lama `test_bupot_draft` (bupot manual) harus tetap 200.000.
- `source_line_ids`: untuk `origin == "bill"` → `move_id._coretax_pph_groups().get(tax_object_id)`, selain itu kosong.
- `tax_line_ids`: journal item `move_id._coretax_pph_family().line_ids` dengan `tax_line_id.coretax_tax_object_id` terisi dan `coretax_tax_object_id == tax_object_id`.
- `pph_gl = -sum(tax_line_ids.mapped("balance"))`; `pph_selisih = pph_gl - pph_estimasi`.
- `unlink`: bila ada record `origin == "bill"` dan context tidak memuat `coretax_bupot_sync` → `UserError("Bukti potong dari tagihan vendor mengikuti tagihannya. Ubah, batalkan, atau hapus tagihannya.")`.
- `_check_before_send`: tambah masalah "Tagihan vendor %s belum diposting." bila `origin == "bill"` dan `move_id.state != "posted"`.
- `_coretax_is_at_aksatax`: `bool(self.aksatax_id) and self.state not in INACTIVE_STATES`.

- [ ] **Step 4: Implementasi `_coretax_pph_groups`**

Ambil baris `display_type == "product"` dari family yang `tax_ids` memuat pajak PPh, kelompokkan per `coretax_tax_object_id`. Bila dalam satu kelompok ada lebih dari satu tarif PPh (`abs(amount)` berbeda), lempar `UserError` yang menyebut kode objek dan tarifnya, mis. "Baris dengan kode objek 24-104-01 memakai tarif PPh berbeda (2%, 0%). Samakan pajaknya atau pisahkan ke tagihan lain."

- [ ] **Step 5: Implementasi `_coretax_sync_bupot` dan `_post`**

Untuk tiap bill posted di `self` (lewati yang tidak posted), dengan `Bupot = self.env["coretax.bupot"].with_context(coretax_bupot_sync=True)`:

```text
groups  = bill._coretax_pph_groups()
active  = bill.coretax_bupot_ids where origin == "bill" and state not in INACTIVE_STATES
for tax_object, lines in groups:
    dpp  = company_currency.round(sum(lines.balance))
    vals = {partner_id: commercial partner, jenis: tax_object.jenis, tarif: abs(pph tax amount),
            jumlah_bruto: dpp, tanggal_pemotongan: invoice_date or date, dok_jenis: "02",
            dok_nomor: (ref or name)[:100], dok_tanggal: invoice_date or date}
    bupot = active with this tax_object
    if not bupot:                          create if dpp > 0
                                           (+ move_id, company_id, origin "bill", tax_object_id,
                                              fasilitas "SKB" if tarif == 0 else "NONE")
    elif bupot.state in EDITABLE_STATES:   write(vals) if dpp > 0, else unlink + note on bill chatter
    elif jumlah_bruto != dpp:              Task 4 (sudah di AksaTax: peringatan + aktivitas)
stale = active, state in EDITABLE_STATES, tax_object not in groups → unlink + note on bill chatter
```

Saat update, `fasilitas`/`nomor_fasilitas` tidak ditimpa. `_post`: setelah `super()`, panggil `_coretax_sync_bupot()` pada bill `in_invoice`/`in_receipt` yang baru diposting dan pada `reversed_entry_id` (posted, `in_invoice`/`in_receipt`) dari `in_refund` yang baru diposting.

- [ ] **Step 6: Jalankan semua test, pastikan lulus**

Run `TAGS=/coretax_integration`. Expected: `0 failed, 0 error(s)`.

- [ ] **Step 7: Commit**

```bash
git add models/ tests/
git commit -m "feat(pph): bukti potong otomatis per kode objek saat tagihan vendor diposting"
```

---

### Task 4: Reset, cancel, hapus, dan credit note

**Files:**
- Modify: `models/account_move_pph.py`
- Modify: `tests/test_pph_bupot.py`

**Interfaces:**
- Consumes: `_coretax_sync_bupot`, `coretax_bupot_ids`, `_coretax_is_at_aksatax`, `EDITABLE_STATES`, `INACTIVE_STATES` (Task 3).
- Produces: `account.move._coretax_bupot_guard(self, action_label: str) -> None`; override `button_draft`, `button_cancel`, `unlink`; cabang "sudah di AksaTax" pada `_coretax_sync_bupot`.

- [ ] **Step 1: Tulis test yang gagal** (tambahkan ke `TestPphBupot`; `_teknik(bill)` = bupot 24-104-01 bill itu):

```python
def _teknik(self, bill):
    return bill.coretax_bupot_ids.filtered(lambda b: b.tax_object_id == self.obj_teknik and b.state != "rejected")

def test_repost_updates_same_bupot(self):
    bill = self._three_lines()
    before = self._teknik(bill)
    bill.button_draft()
    bill.invoice_line_ids[0].price_unit = 20_000_000
    bill.action_post()
    self.assertEqual(self._teknik(bill), before)
    self.assertEqual(before.jumlah_bruto, 25_000_000)

def test_repost_without_pph_deletes_bupot(self):
    bill = self._three_lines()
    bill.button_draft()
    bill.invoice_line_ids[2].tax_ids = False
    bill.action_post()
    self.assertEqual(bill.coretax_bupot_ids.tax_object_id, self.obj_teknik)

def test_reset_and_cancel_blocked_when_bupot_at_aksatax(self):
    bill = self._three_lines()
    self._teknik(bill).write({"aksatax_id": "x-1", "state": "draft"})
    with self.assertRaisesRegex(UserError, "AksaTax"):
        bill.button_draft()
    with self.assertRaisesRegex(UserError, "AksaTax"):
        bill.button_cancel()

def test_cancel_and_unlink_delete_new_bupots(self):
    bill = self._three_lines()
    bill.button_draft()
    bill.button_cancel()
    self.assertFalse(bill.coretax_bupot_ids)
    other = self._three_lines()
    other.button_draft()
    bupot_ids = other.coretax_bupot_ids.ids
    other.unlink()
    self.assertFalse(self.env["coretax.bupot"].browse(bupot_ids).exists())

def test_repost_after_rejected_creates_new_bupot(self):
    bill = self._three_lines()
    old = self._teknik(bill)
    old.write({"aksatax_id": "x-1", "state": "rejected"})
    bill.button_draft()
    bill.action_post()
    new = self._teknik(bill)
    self.assertTrue(new and new != old)
    self.assertEqual(old.state, "rejected")

def test_partial_credit_note_nets_dpp_and_reset_restores(self):
    bill = self._three_lines()
    refund = bill._reverse_moves([{"invoice_date": bill.invoice_date}])
    refund.invoice_line_ids[1:].unlink()
    refund.invoice_line_ids.price_unit = 4_000_000
    refund.action_post()
    self.assertRecordValues(self._teknik(bill), [{"jumlah_bruto": 11_000_000, "pph_gl": 220_000}])
    self.assertEqual(bill.coretax_bupot_ids.filtered(lambda b: b.tax_object_id == self.obj_sewa).jumlah_bruto, 20_000_000)
    refund.button_draft()
    self.assertEqual(self._teknik(bill).jumlah_bruto, 15_000_000)

def test_full_credit_note_deletes_new_bupots(self):
    bill = self._three_lines()
    bill._reverse_moves([{"invoice_date": bill.invoice_date}], cancel=True)
    self.assertFalse(bill.coretax_bupot_ids)

def test_credit_note_on_sent_bupot_schedules_activity(self):
    bill = self._three_lines()
    sent = self._teknik(bill)
    sent.write({"aksatax_id": "x-1", "state": "approved"})
    bill._reverse_moves([{"invoice_date": bill.invoice_date}], cancel=True)
    self.assertEqual(sent.jumlah_bruto, 15_000_000)
    self.assertTrue(bill.activity_ids.filtered(lambda a: sent.name in (a.summary or "")))
```

Bila Odoo menolak `other.unlink()` karena aturan urutan nomor dokumen (bukan karena kode kita), pakai `other.with_context(force_delete=True).unlink()`; yang diuji adalah bupot ikut terhapus.

- [ ] **Step 2: Jalankan, pastikan gagal**

Run `TAGS=/coretax_integration:TestPphBupot`. Expected: test blokir/hapus/credit note gagal.

- [ ] **Step 3: Implementasi**

- `_coretax_bupot_guard(action_label)`: bila ada bupot `origin == "bill"` dengan `_coretax_is_at_aksatax()` → `UserError` ala `_coretax_guard` di `models/account_move.py`, mis. "Tagihan %(moves)s punya bukti potong yang sudah ada di AksaTax (%(bupots)s). Batalkan dulu bukti potongnya di AksaTax sebelum %(action)s tagihan ini."
- `button_draft`: guard → simpan induk posted dari `in_refund` di `self` → `super()` → `induk._coretax_sync_bupot()`.
- `button_cancel`: guard → simpan induk refund → `super()` → hapus bupot `origin == "bill"` berstate `EDITABLE_STATES` milik `self` (context `coretax_bupot_sync`) + catatan chatter bill → `induk._coretax_sync_bupot()`.
- `unlink`: guard → hapus bupot editable milik `self` (context `coretax_bupot_sync`) → `super()`.
- Cabang "sudah di AksaTax" di `_coretax_sync_bupot` (bila `jumlah_bruto` ≠ DPP baru): `bupot.message_post` berisi DPP lama vs baru, dan aktivitas `mail.mail_activity_data_todo` di bill untuk `invoice_user_id or create_uid` (lewati bila `base.user_root`, sama dengan `coretax.faktur._schedule_fix_activity`), `summary=_("Pembetulan bukti potong %s", bupot.name)`, note meminta pembetulan/pembatalan di AksaTax.

- [ ] **Step 4: Jalankan semua test, pastikan lulus**

Run `TAGS=/coretax_integration`. Expected: `0 failed, 0 error(s)`.

- [ ] **Step 5: Commit**

```bash
git add models/account_move_pph.py tests/test_pph_bupot.py
git commit -m "feat(pph): kontrol reset/cancel/hapus bill dan credit note pada bukti potong"
```

---

### Task 5: Tampilan dan deskripsi modul

**Files:**
- Create: `views/account_tax_views.xml`
- Modify: `views/account_move_views.xml`
- Modify: `views/coretax_bupot_views.xml`
- Modify: `models/account_move_pph.py`
- Modify: `__manifest__.py` (tambahkan view baru di `data` setelah `views/account_move_views.xml`; tambahkan butir PPh di `description`)
- Modify: `tests/test_pph_tax.py`

**Interfaces:**
- Consumes: semua field dari Task 1–4.
- Produces: `account.move.coretax_bupot_count` (Integer compute), `account.move.coretax_has_pph` (Boolean compute: ada baris produk dengan `coretax_tax_object_id` dan pajak PPh), `account.move.action_coretax_view_bupot() -> dict` (pola `action_coretax_view_faktur`, `action_coretax_bupot`, domain `move_id`, buka form bila satu).

- [ ] **Step 1: Tulis test yang gagal** (tambahkan ke `TestPphTax`):

```python
def test_bill_form_fills_object_and_buttons(self):
    with Form(self.env["account.move"].with_context(default_move_type="in_invoice")) as form:
        form.partner_id = self.partner_a
        form.invoice_date = "2026-10-01"
        with form.invoice_line_ids.new() as line:
            line.product_id = self.product_a
            line.price_unit = 20_000_000
            line.tax_ids.clear()
            line.tax_ids.add(self.pph42)
            self.assertEqual(line.coretax_tax_object_id, self.obj_sewa)
    bill = form.save()
    bill.action_post()
    self.assertTrue(bill.coretax_has_pph)
    self.assertEqual(bill.coretax_bupot_count, 1)
    action = bill.action_coretax_view_bupot()
    self.assertEqual(action["res_id"], bill.coretax_bupot_ids.id)
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run `TAGS=/coretax_integration:TestPphTax.test_bill_form_fills_object_and_buttons`. Expected: gagal karena `coretax_tax_object_id` tidak ada di view baris bill.

- [ ] **Step 3: Implementasi view dan field**

- `views/account_tax_views.xml`: inherit `account.view_tax_form`, `coretax_tax_object_id` setelah `invoice_label` di halaman `advanced_options`, `invisible="type_tax_use != 'purchase'"`, `options="{'no_create': True}"`.
- `views/account_move_views.xml` (inherit `account.view_move_form` yang sudah ada):
  - di list `invoice_line_ids` setelah `tax_ids`: `coretax_tax_object_id`, `optional="show"`, `column_invisible="parent.move_type not in ('in_invoice', 'in_receipt', 'in_refund')"`, `options="{'no_create': True}"`;
  - stat button `action_coretax_view_bupot` (ikon `fa-file-text`, teks "Bukti Potong", nilai `coretax_bupot_count`, `invisible="not coretax_bupot_count"`);
  - tombol `action_coretax_create_bupot`: tambahkan `or coretax_has_pph` ke `invisible`.
- `views/coretax_bupot_views.xml`:
  - `partner_id`, `jenis`, `tax_object_id`, `move_id` readonly juga bila `origin == 'bill'`;
  - `origin` tampil readonly di grup "Dokumen dasar";
  - `<notebook>` dengan page "Sumber" (`invisible="origin != 'bill'"`): `source_line_ids` (list readonly: `move_id`, `product_id`, `name`, `balance` string "DPP (IDR)"), `tax_line_ids` (list readonly: `move_id`, `account_id`, `balance`), dan `pph_gl`, `pph_selisih`;
  - alert warning bila `pph_selisih <= -1 or pph_selisih >= 1`: "PPh di jurnal berbeda dengan perkiraan PPh DJP (dibulatkan ke bawah). Periksa sebelum mengirim.";
  - search: filter `from_bill` "Dari tagihan" dan `manual` "Manual" (domain `origin`). Group by kode objek sudah ada.

- [ ] **Step 4: Jalankan semua test, pastikan lulus**

Run `TAGS=/coretax_integration`. Expected: `0 failed, 0 error(s)`, tanpa warning view di log (`grep -E "WARNING.*view"` kosong).

- [ ] **Step 5: Uji instal bersih**

Run perintah test dengan `-d coretax_pph_fresh -i coretax_integration` (DB baru) dan `TAGS=/coretax_integration`. Expected: `0 failed, 0 error(s)`. Lalu `dropdb coretax_pph_fresh`.

- [ ] **Step 6: Commit**

```bash
git add views/ models/account_move_pph.py __manifest__.py tests/test_pph_tax.py
git commit -m "feat(pph): tampilan kode objek di baris bill, smart button dan tab sumber bukti potong"
```
