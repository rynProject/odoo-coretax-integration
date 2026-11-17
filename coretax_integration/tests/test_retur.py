from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import FAKTUR_ID, CoretaxTestCommon, faktur_body

RETUR_ID = "33333333-3333-4333-8333-333333333333"
FM_ID = "44444444-4444-4444-8444-444444444444"
# Nomor faktur vendor di tagihan uji: 040.002-26.00000099 (16 digit lama) -> NSFP 0022600000099.
FM_NOMOR = "04000022600000099"
KELUARAN_MESSAGE = "Retur atas faktur keluaran dibuat oleh pembeli di Coretax, bukan dari Odoo."


def retur_body(status="DRAFT", **extra):
    return {"id": RETUR_ID, "sisi": "MASUKAN", "status": status, "nomorDokumenRetur": "RTR/001",
            "nomorFaktur": FM_NOMOR, "tanggalRetur": "2026-10-05", "masaPajakRetur": 10,
            "tahunPajakRetur": 2026, "nilaiReturDpp": 10000, "nilaiReturPpn": 1200, "nilaiReturPpnbm": 0,
            "referensi": "RTR/001", "lastError": None, **extra}


def fm_row(**extra):
    return {"id": FM_ID, "nomorFaktur": FM_NOMOR, "tanggalFaktur": "2026-10-01", "npwpPenjual": "1234567890123457",
            "namaPenjual": "PT Vendor", "masaPajak": 10, "tahunPajak": 2026, "nilaiDpp": 150000, "nilaiPpn": 16500,
            "status": "DIKREDITKAN", "masaKredit": 10, "tahunKredit": 2026, **extra}


def fm_items(first_name="product_a", done=None):
    zero = {"returJmlBrgJasa": 0, "returDiskon": 0, "returDpp": 0, "returDppLain": 0, "returPpn": 0, "returPpnbm": 0}
    return {"approvalSign": "sig", "items": [
        {"brgJasa": "A", "kdBrgJasa": "000000", "namaBrgJasa": first_name, "satuanBrgJasa": "UM.0018",
         "hargaSatuan": 100000, "jmlBrgJasa": 1, "totalHarga": 100000, "diskon": 0, "dpp": 100000,
         "dppLain": 91666.67, "tarifPpn": 12, "ppn": 11000, "tarifPpnbm": 0, "ppnbm": 0, "cekDppLain": True},
        {"brgJasa": "B", "kdBrgJasa": "000000", "namaBrgJasa": "Jasa lain", "satuanBrgJasa": "UM.0033",
         "hargaSatuan": 25000, "jmlBrgJasa": 2, "totalHarga": 50000, "diskon": 0, "dpp": 50000,
         "dppLain": 45833.33, "tarifPpn": 12, "ppn": 5500, "tarifPpnbm": 0, "ppnbm": 0, "cekDppLain": True},
    ], "sudahDiretur": done or [zero, dict(zero)]}


def lookup_routes(extra=None, items=None):
    """Rute pencarian faktur masukan + barang/jasanya, ditambah rute lain (`extra`)."""
    return {
        ("GET", "/nota-retur/faktur-masukan"): (200, {"data": [fm_row()]}),
        ("GET", f"/nota-retur/faktur-masukan/{FM_ID}/items"): (200, items or fm_items()),
        **(extra or {}),
    }


@tagged("post_install", "-at_install")
class TestRetur(CoretaxTestCommon):

    def _credit_note(self, invoice):
        reverse = invoice._reverse_moves([{"invoice_date": "2026-10-05", "ref": "RTR/001"}])
        reverse.action_post()
        return reverse

    def _out_retur(self, **vals):
        invoice = self._invoice()
        faktur = self.env["coretax.faktur"].create({
            "move_id": invoice.id, "state": "approved", "aksatax_id": FAKTUR_ID, "nomor_faktur": "0400260000000001"})
        credit = self._credit_note(invoice)
        return self.env["coretax.retur"].create({
            "sisi": "keluaran", "move_id": credit.id, "faktur_id": faktur.id, "nomor_faktur": faktur.nomor_faktur,
            "nomor_dokumen_retur": "RTR/001", "tanggal_retur": "2026-10-05",
            "nilai_dpp": 10000, "nilai_ppn": 1200, **vals})

    def _in_retur(self, **vals):
        bill = self._bill_vendor()
        credit = self._credit_note(bill)
        return self.env["coretax.retur"].create({
            "sisi": "masukan", "move_id": credit.id, "nomor_faktur": FM_NOMOR, "faktur_masukan_id": FM_ID,
            "nomor_dokumen_retur": "RTR/002", "tanggal_retur": "2026-10-05", "is_creditable": True,
            "line_ids": [(0, 0, {"index": 0, "nama": "product_a", "retur_jumlah": 1, "retur_dpp": 10000,
                                 "retur_dpp_lain": 9166.67, "retur_ppn": 1100})],
            **vals})

    def _bill_vendor(self):
        self.partner_a.street = "Jl. Vendor 1"
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.partner_a.id, "invoice_date": "2026-10-01",
            "coretax_vendor_tax_number": "040.002-26.00000099",
            "invoice_line_ids": [(0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 100000})],
        })
        bill.action_post()
        return bill

    def test_origin_and_name(self):
        retur = self._out_retur()
        self.assertEqual(retur.origin_move_id, retur.faktur_id.move_id)
        self.assertEqual(retur.name, "RTR/001")
        self.assertEqual((retur.masa, retur.tahun), (10, 2026))
        self.assertEqual(retur.state, "new")

    def test_keluaran_not_sent(self):
        retur = self._out_retur()
        fake, patcher = self._patch({})
        with patcher, self.assertRaisesRegex(UserError, KELUARAN_MESSAGE):
            retur.action_send()
        with self.assertRaisesRegex(UserError, KELUARAN_MESSAGE):
            retur._payload()
        self.assertFalse(fake.calls)

    def test_payload_masukan(self):
        retur = self._in_retur()
        retur.write({"line_ids": [(0, 0, {"index": 1, "nama": "kosong"})]})
        payload = retur._payload()
        self.assertEqual(payload, {
            "sisi": "MASUKAN", "fakturMasukanId": FM_ID, "tanggalRetur": "2026-10-05", "isCreditable": True,
            "nomorDokumenRetur": "RTR/002", "referensi": retur.move_id.name,
            "items": [{"index": 0, "returJmlBrgJasa": 1.0, "returDiskon": 0.0, "returDpp": 10000.0,
                       "returDppLain": 9166.67, "returPpn": 1100.0, "returPpnbm": 0.0}],
        })
        self.assertEqual((retur.nilai_dpp, retur.nilai_ppn), (10000, 1100))

    def test_totals_follow_lines(self):
        retur = self._in_retur()
        retur.write({"line_ids": [(1, retur.line_ids.id, {"retur_dpp": 5000, "retur_ppn": 550})]})
        self.assertEqual((retur.nilai_dpp, retur.nilai_ppn), (5000, 550))

    def test_masukan_needs_value_and_faktur(self):
        retur = self._in_retur(faktur_masukan_id=False)
        retur.line_ids.retur_dpp = 0
        problems = retur._check_before_send()
        self.assertIn("Faktur masukan asal belum dipilih", problems[0])
        self.assertIn("DPP retur harus lebih dari 0", problems[1])

    def test_send_success(self):
        retur = self._in_retur()
        fake, patcher = self._patch({
            ("POST", "/nota-retur"): (201, retur_body()),
            ("POST", f"/nota-retur/{RETUR_ID}/kirim"): (202, {"id": RETUR_ID, "status": "SUBMITTED"}),
        })
        with patcher:
            retur.action_send()
        self.assertEqual(retur.state, "submitted")
        self.assertEqual(retur.aksatax_id, RETUR_ID)
        self.assertEqual(retur.send_attempt, 1)
        base = retur._idempotency_base()
        self.assertEqual([c["headers"]["Idempotency-Key"] for c in fake.calls], [base, f"{base}-kirim-1"])

    def test_create_failure_sets_error_and_bumps_version(self):
        retur = self._in_retur()
        _fake, patcher = self._patch({
            ("POST", "/nota-retur"): (422, {"error": {"code": "RETUR_INVALID", "message": "Nilai retur melebihi sisa faktur"}}),
        })
        with patcher:
            retur.action_send()
        self.assertEqual(retur.state, "error")
        self.assertEqual(retur.error_code, "RETUR_INVALID")
        self.assertIn("melebihi", retur.last_error)
        self.assertEqual(retur.version, 2)
        self.assertFalse(retur.aksatax_id)

    def test_send_failure_keeps_draft(self):
        retur = self._in_retur()
        _fake, patcher = self._patch({
            ("POST", "/nota-retur"): (201, retur_body()),
            ("POST", f"/nota-retur/{RETUR_ID}/kirim"): (409, {"error": {"code": "CONFLICT", "message": "Status tidak sesuai"}}),
        })
        with patcher:
            retur.action_send()
        self.assertEqual(retur.state, "draft")
        self.assertEqual(retur.aksatax_id, RETUR_ID)
        self.assertTrue(retur.last_error)
        self.assertEqual(retur.version, 1)

    def test_retur_on_old_aksatax(self):
        retur = self._in_retur()
        _fake, patcher = self._patch({})
        with patcher:
            retur.action_send()
        self.assertEqual(retur.state, "error")
        self.assertIn("butuh PajakMu versi terbaru", retur.last_error)
        self.assertEqual(retur.error_code, "NOT_SUPPORTED")

    def test_event_approved(self):
        retur = self._out_retur(state="submitted", aksatax_id=RETUR_ID)
        _fake, patcher = self._patch({
            ("GET", f"/nota-retur/{RETUR_ID}"): (200, retur_body("APPROVED")),
            ("GET", "/events"): (200, {"data": [
                {"id": "evt_1", "sequence": 1, "type": "retur.approved", "object": {"type": "nota_retur", "id": RETUR_ID},
                 "data": {"id": RETUR_ID, "nomor": "RTR/001", "status": "APPROVED"}},
            ], "next_after": 1, "has_more": False}),
        })
        with patcher:
            summary = self.connection._sync()
        self.assertEqual(summary["events"], 1)
        self.assertEqual(retur.state, "approved")
        self.assertIn("Nota retur RTR/001 disetujui DJP.", retur.move_id.message_ids[0].body)

    def test_event_unknown_retur_ignored(self):
        _fake, patcher = self._patch({
            ("GET", "/events"): (200, {"data": [
                {"id": "evt_1", "sequence": 1, "type": "retur.approved", "object": {"type": "nota_retur", "id": "lain"},
                 "data": {"id": "lain", "status": "APPROVED"}},
            ], "next_after": 1, "has_more": False}),
        })
        with patcher:
            summary = self.connection._sync()
        self.assertEqual(summary["events"], 1)

    def test_refresh_pending_cancel_rejected(self):
        retur = self._out_retur(state="cancel_requested", aksatax_id=RETUR_ID,
                                last_sync=fields.Datetime.now() - timedelta(hours=1))
        _fake, patcher = self._patch({
            ("GET", f"/nota-retur/{RETUR_ID}"): (200, retur_body("APPROVED", lastError="Pembatalan ditolak DJP")),
            ("GET", "/events"): (200, {"data": [], "next_after": 0, "has_more": False}),
        })
        with patcher:
            self.connection._sync()
        self.assertEqual(retur.state, "approved")
        self.assertEqual(retur.last_error, "Pembatalan ditolak DJP")

    def test_vendor_tax_number_constraint(self):
        bill = self._bill_vendor()
        for good in ("0400022600000", "0400022600000099", "04000226000000099"):
            bill.coretax_vendor_tax_number = good
        with self.assertRaisesRegex(ValidationError, "13, 16, atau 17 digit"):
            bill.coretax_vendor_tax_number = "123"
        bill.coretax_vendor_tax_number = False

    # ── wizard kirim, kunci, wizard batal (C2) ───────────────────────
    def _wizard(self, move, **vals):
        wizard = self.env["coretax.retur.wizard"].with_context(active_model="account.move", active_id=move.id).create(vals)
        return wizard

    def _out_credit(self, approved=True):
        invoice = self._invoice()
        if approved:
            self.env["coretax.faktur"].create({
                "move_id": invoice.id, "state": "approved", "aksatax_id": FAKTUR_ID,
                "nomor_faktur": "0400260000000001"})
        return self._credit_note(invoice)

    def _in_credit(self, vendor_number="040.002-26.00000099"):
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.partner_a.id, "invoice_date": "2026-10-01",
            "coretax_vendor_tax_number": vendor_number,
            "invoice_line_ids": [(0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 100000,
                                         "tax_ids": [(6, 0, self.tax_purchase_a.ids)]})],
        })
        bill.action_post()
        return self._credit_note(bill)

    def test_amounts_from_credit_note(self):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner_a.id, "invoice_date": "2026-10-01",
            "invoice_line_ids": [(0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 1000000,
                                         "tax_ids": [(6, 0, self.tax_sale_a.ids)]})],
        })
        invoice.action_post()
        credit = self._credit_note(invoice)
        self.assertEqual(credit._coretax_retur_amounts(), {"dpp": 1000000, "ppn": 120000, "ppnbm": 0, "tarif": 12})

    def test_wizard_defaults_and_name_match(self):
        credit = self._in_credit()
        fake, patcher = self._patch(lookup_routes())
        with patcher:
            wizard = self._wizard(credit)
        self.assertEqual(fake.calls[0]["params"], {"q": "0022600000099"})
        self.assertEqual(wizard.nomor_dokumen_retur, credit.name)
        self.assertEqual(wizard.tanggal_retur, credit.invoice_date)
        self.assertEqual(wizard.referensi, credit.name)
        self.assertEqual(wizard.faktur_masukan_id, FM_ID)
        self.assertEqual(wizard.faktur_masukan_nomor, FM_NOMOR)
        self.assertTrue(wizard.is_creditable)
        self.assertFalse(wizard.lookup_message)
        first, second = wizard.line_ids.sorted("index")
        # baris nota kredit "product_a" cocok dengan barang/jasa pertama: nilainya dipakai, yang lain 0
        self.assertEqual((first.retur_jumlah, first.retur_dpp, first.retur_dpp_lain, first.retur_ppn),
                         (1, 100000, 91666.67, 11000))
        self.assertEqual((second.retur_dpp, second.retur_ppn, second.sisa_dpp), (0, 0, 50000))
        self.assertEqual(wizard.total_dpp, 100000)

    def test_wizard_proportional_prefill_and_remainder(self):
        credit = self._in_credit()
        half = {"returJmlBrgJasa": 0, "returDiskon": 0, "returDpp": 20000, "returDppLain": 18333.33,
                "returPpn": 2200, "returPpnbm": 0}
        zero = dict(half, returDpp=0, returDppLain=0, returPpn=0)
        _fake, patcher = self._patch(lookup_routes(items=fm_items(first_name="Barang lain", done=[half, zero])))
        with patcher:
            wizard = self._wizard(credit)
        first, second = wizard.line_ids.sorted("index")
        self.assertEqual((first.sisa_dpp, first.sisa_ppn), (80000, 8800))
        credit_dpp = credit._coretax_retur_amounts()["dpp"]
        ratio = min(1.0, credit_dpp / 130000)
        self.assertEqual(first.retur_dpp, round(80000 * ratio, 2))
        self.assertEqual(second.retur_dpp, round(50000 * ratio, 2))

    def test_wizard_masukan_sends(self):
        credit = self._in_credit()
        fake, patcher = self._patch(lookup_routes({
            ("POST", "/nota-retur"): (201, retur_body()),
            ("POST", f"/nota-retur/{RETUR_ID}/kirim"): (202, {"id": RETUR_ID, "status": "SUBMITTED"}),
        }))
        with patcher:
            action = self._wizard(credit).action_confirm()
        retur = self.env["coretax.retur"].browse(action["res_id"])
        self.assertEqual((retur.state, retur.sisi), ("submitted", "masukan"))
        self.assertEqual((retur.faktur_masukan_id, retur.nomor_faktur), (FM_ID, FM_NOMOR))
        self.assertEqual(retur.nilai_dpp, 100000)
        sent = next(c["json"] for c in fake.calls if c["method"] == "POST" and c["path"] == "/nota-retur")
        self.assertEqual((sent["sisi"], sent["fakturMasukanId"], sent["isCreditable"]), ("MASUKAN", FM_ID, True))
        self.assertEqual([item["index"] for item in sent["items"]], [0])
        self.assertEqual(credit.coretax_retur_count, 1)

    def test_wizard_keluaran_blocked(self):
        credit = self._out_credit()
        _fake, patcher = self._patch({})
        with patcher, self.assertRaisesRegex(UserError, KELUARAN_MESSAGE):
            self._wizard(credit).action_confirm()

    def test_wizard_requires_posted(self):
        credit = self._in_credit()
        _fake, patcher = self._patch(lookup_routes())
        with patcher:
            wizard = self._wizard(credit)
        credit.button_draft()
        with self.assertRaisesRegex(UserError, "Posting nota kredit vendor dulu, lalu kirim nota retur"):
            wizard.action_confirm()

    def test_wizard_requires_origin(self):
        credit = self.env["account.move"].create({
            "move_type": "in_refund", "partner_id": self.partner_a.id, "invoice_date": "2026-10-05",
            "invoice_line_ids": [(0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 1000})],
        })
        credit.action_post()
        with self.assertRaisesRegex(UserError, "tidak terhubung ke tagihan asal"):
            self._wizard(credit).action_confirm()

    def test_wizard_rejects_existing_live_retur(self):
        retur = self._in_retur(state="approved", aksatax_id=RETUR_ID)
        _fake, patcher = self._patch(lookup_routes())
        with patcher, self.assertRaisesRegex(UserError, "sudah punya nota retur RTR/002"):
            self._wizard(retur.move_id).action_confirm()

    def test_wizard_masukan_needs_vendor_number(self):
        credit = self._in_credit(vendor_number=False)
        wizard = self._wizard(credit)
        self.assertFalse(wizard.faktur_masukan_id)
        with self.assertRaisesRegex(UserError, "Faktur masukan asal belum ditemukan di PajakMu"):
            wizard.action_confirm()
        with self.assertRaisesRegex(UserError, "Isi Nomor Faktur Pajak vendor dulu"):
            wizard.action_lookup()

    def test_wizard_not_found_lists_vendor_fakturs(self):
        credit = self._in_credit()
        other = fm_row(nomorFaktur="04002600000000123")

        def search(params, _body):
            return 200, {"data": [other] if params == {"q": "1234567890123457"} else []}

        _fake, patcher = self._patch({("GET", "/nota-retur/faktur-masukan"): search})
        with patcher:
            wizard = self._wizard(credit)
        self.assertFalse(wizard.faktur_masukan_id)
        self.assertIn("Faktur masukan 040.002-26.00000099 tidak ditemukan di PajakMu", wizard.lookup_message)
        self.assertIn("04002600000000123", wizard.lookup_message)
        with self.assertRaisesRegex(UserError, "tidak ditemukan di PajakMu"):
            wizard.action_confirm()

    def test_wizard_lookup_again(self):
        credit = self._in_credit(vendor_number=False)
        wizard = self._wizard(credit)
        wizard.nomor_faktur = FM_NOMOR
        _fake, patcher = self._patch(lookup_routes())
        with patcher:
            action = wizard.action_lookup()
        self.assertEqual((action["res_id"], action["target"]), (wizard.id, "new"))
        self.assertEqual(wizard.faktur_masukan_id, FM_ID)
        self.assertEqual(len(wizard.line_ids), 2)

    def test_wizard_old_server_message(self):
        credit = self._in_credit()
        _fake, patcher = self._patch({})
        with patcher:
            wizard = self._wizard(credit)
        self.assertIn("butuh PajakMu versi terbaru", wizard.lookup_message)

    def test_wizard_dpp_must_be_positive(self):
        credit = self._in_credit()
        _fake, patcher = self._patch(lookup_routes())
        with patcher:
            wizard = self._wizard(credit)
        wizard.line_ids.write({"retur_dpp": 0, "retur_dpp_lain": 0, "retur_ppn": 0, "retur_jumlah": 0})
        with self.assertRaisesRegex(UserError, "DPP retur harus lebih dari 0"):
            wizard.action_confirm()

    def test_wizard_rejects_more_than_remainder(self):
        credit = self._in_credit()
        _fake, patcher = self._patch(lookup_routes())
        with patcher:
            wizard = self._wizard(credit)
        wizard.line_ids.sorted("index")[0].retur_ppn = 11001
        with self.assertRaisesRegex(UserError, "product_a: PPN retur melebihi sisa faktur asal"):
            wizard.action_confirm()

    def test_credit_note_locked_while_retur_live(self):
        retur = self._out_retur(state="approved", aksatax_id=RETUR_ID)
        credit = retur.move_id
        with self.assertRaisesRegex(UserError, "punya nota retur RTR/001 di PajakMu"):
            credit.button_draft()
        with self.assertRaisesRegex(UserError, "Batalkan nota retur dulu"):
            credit.button_cancel()
        with self.assertRaisesRegex(UserError, "Batalkan nota retur dulu"):
            credit.unlink()
        self.assertTrue(credit.coretax_retur_live)
        retur.state = "cancelled"
        self.assertFalse(credit.coretax_retur_live)
        credit.button_draft()
        self.assertEqual(credit.state, "draft")

    def test_cancel_wizard(self):
        retur = self._out_retur(state="approved", aksatax_id=RETUR_ID)
        action = retur.action_open_cancel_wizard()
        self.assertEqual(action["res_model"], "coretax.retur.cancel.wizard")
        wizard = self.env["coretax.retur.cancel.wizard"].create({"retur_id": retur.id, "alasan": "Salah nilai"})
        fake, patcher = self._patch({
            ("POST", f"/nota-retur/{RETUR_ID}/batal"): (202, {"id": RETUR_ID, "status": "CANCEL_REQUESTED"}),
        })
        with patcher:
            wizard.action_confirm()
        self.assertEqual(retur.state, "cancel_requested")
        self.assertEqual(retur.alasan_batal, "Salah nilai")
        self.assertEqual(retur.cancel_attempt, 1)
        key = fake.calls[0]["headers"]["Idempotency-Key"]
        self.assertTrue(key.startswith(retur._idempotency_base() + "-batal-1-"))
        self.assertEqual(fake.calls[0]["json"], {"alasan": "Salah nilai"})

    def test_cancel_wizard_short_reason(self):
        retur = self._out_retur(state="approved", aksatax_id=RETUR_ID)
        wizard = self.env["coretax.retur.cancel.wizard"].create({"retur_id": retur.id, "alasan": "abc"})
        with self.assertRaisesRegex(UserError, "minimal 5 karakter"):
            wizard.action_confirm()

    def test_smart_button_count(self):
        retur = self._out_retur()
        credit = retur.move_id
        self.assertEqual(credit.coretax_retur_count, 1)
        action = credit.action_coretax_view_retur()
        self.assertEqual(action["res_id"], retur.id)
        self.assertEqual(credit.action_coretax_open_retur_wizard()["res_model"], "coretax.retur.wizard")

    def test_unlink_credit_note_deletes_unsent_retur(self):
        retur = self._out_retur(state="error")
        credit = retur.move_id
        credit.button_draft()
        credit.unlink()
        self.assertFalse(retur.exists())
        self.assertFalse(credit.exists())

    def test_unlink_credit_note_blocked_by_retur_history(self):
        retur = self._out_retur(state="cancelled", aksatax_id=RETUR_ID)
        credit = retur.move_id
        credit.button_draft()
        with self.assertRaisesRegex(UserError, "punya riwayat nota retur RTR/001 di PajakMu, jadi tidak bisa "
                                               "dihapus. Batalkan nota kredit saja."):
            credit.unlink()
        self.assertTrue(retur.exists())

    def test_amounts_ppnbm_and_pph(self):
        template = self.env["account.chart.template"].with_company(self.company_data["company"])
        stlg = template.ref("l10n_id_tax_group_stlg", raise_if_not_found=False)
        self.assertTrue(stlg, "grup STLG tidak ada di chart uji")
        stlg_tax = self.env["account.tax"].search([
            ("tax_group_id", "=", stlg.id), ("type_tax_use", "=", "purchase"),
            ("company_id", "=", self.company_data["company"].id)], limit=1)
        if not stlg_tax:
            stlg_tax = self.env["account.tax"].create({
                "name": "STLG uji", "amount": 20, "amount_type": "percent", "type_tax_use": "purchase",
                "tax_group_id": stlg.id, "company_id": self.company_data["company"].id})
        pph = template.ref("tax_pph23_2")
        self.tax_purchase_a.amount = 12.0
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.partner_a.id, "invoice_date": "2026-10-01",
            "invoice_line_ids": [
                (0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 100000,
                        "tax_ids": [(6, 0, (self.tax_purchase_a + pph).ids)]}),
                (0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 50000,
                        "tax_ids": [(6, 0, stlg_tax.ids)]}),
            ],
        })
        bill.action_post()
        credit = self._credit_note(bill)
        self.assertEqual(credit._coretax_retur_amounts(), {"dpp": 100000, "ppn": 12000, "ppnbm": 10000, "tarif": 12})

    # ── Track O: nilai sesuai PajakMu, kunci, wizard ─────────────────
    def _sale_credit(self, tax, price=1000000, faktur_vals=None, extra_lines=()):
        lines = [(0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": price,
                         "tax_ids": [(6, 0, tax.ids)]})] + list(extra_lines)
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner_a.id, "invoice_date": "2026-10-01",
            "invoice_line_ids": lines})
        invoice.action_post()
        faktur = self.env["coretax.faktur"]
        if faktur_vals is not None:
            faktur = self.env["coretax.faktur"].create({
                "move_id": invoice.id, "state": "approved", "aksatax_id": FAKTUR_ID,
                "nomor_faktur": "0400260000000001", **faktur_vals})
        return self._credit_note(invoice), faktur

    def _chart_tax(self, xmlid):
        return self.env["account.chart.template"].with_company(self.company_data["company"]).ref(xmlid)

    def test_amounts_non_luxury_rounding(self):
        tax = self._chart_tax("tax_ST4")
        tax.amount = 11  # bawaan chart (CoretaxTestCommon mengubahnya jadi 12 untuk uji lain)
        self.assertEqual(tax.tax_group_id.id, self._chart_tax("l10n_id_tax_group_non_luxury_goods").id)
        credit, _faktur = self._sale_credit(tax)
        self.assertEqual(credit._coretax_retur_amounts(), {"dpp": 1000000, "ppn": 109999, "ppnbm": 0, "tarif": 12})
        # retur penuh memakai total e-Faktur apa adanya
        credit, faktur = self._sale_credit(tax, faktur_vals={"total_dpp": 1000000, "total_ppn": 109999})
        self.assertEqual(credit._coretax_retur_amounts(faktur=faktur),
                         {"dpp": 1000000, "ppn": 109999, "ppnbm": 0, "tarif": 12})

    def test_amounts_luxury_and_per_line_floor(self):
        credit, _faktur = self._sale_credit(self._chart_tax("tax_ST3"), price=100001)
        self.assertEqual(credit._coretax_retur_amounts()["ppn"], 12000)
        self._chart_tax("tax_ST4").amount = 11
        line = (0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 999,
                       "tax_ids": [(6, 0, self._chart_tax("tax_ST4").ids)]})
        credit, _faktur = self._sale_credit(self._chart_tax("tax_ST4"), price=999, extra_lines=[line])
        # per baris: floor(floor(999*11/12)*0.12) = 109, dua baris = 218 (bukan 219 dari total)
        self.assertEqual(credit._coretax_retur_amounts()["ppn"], 218)

    def test_amounts_wapu_pair(self):
        company = self.company_data["company"]
        non_luxury = self._chart_tax("tax_ST4")
        group = self.env["account.tax"].create({
            "name": "WAPU uji", "amount_type": "group", "type_tax_use": "sale", "company_id": company.id,
            "tax_group_id": non_luxury.tax_group_id.id,
            "children_tax_ids": [(0, 0, {"name": "WAPU +", "amount_type": "percent", "amount": 11,
                                         "type_tax_use": "sale", "company_id": company.id,
                                         "tax_group_id": non_luxury.tax_group_id.id}),
                                 (0, 0, {"name": "WAPU -", "amount_type": "percent", "amount": -11,
                                         "type_tax_use": "sale", "company_id": company.id,
                                         "tax_group_id": non_luxury.tax_group_id.id})]})
        credit, _faktur = self._sale_credit(group)
        self.assertEqual(credit._coretax_retur_amounts(), {"dpp": 1000000, "ppn": 109999, "ppnbm": 0, "tarif": 12})
        credit, _faktur = self._sale_credit(self._chart_tax("tax_ST5"))  # pemungut: satu pajak +100%/-100%
        self.assertEqual(credit._coretax_retur_amounts()["ppn"], 109999)

    def test_amounts_keluaran_clamped_to_faktur_remainder(self):
        tax = self.tax_sale_a
        credit, faktur = self._sale_credit(tax, faktur_vals={"total_dpp": 1500000, "total_ppn": 180000})
        self.env["coretax.retur"].create({
            "sisi": "keluaran", "move_id": credit.id, "faktur_id": faktur.id, "nomor_faktur": faktur.nomor_faktur,
            "nomor_dokumen_retur": "RTR/OLD", "tanggal_retur": "2026-10-05", "nilai_dpp": 700000,
            "nilai_ppn": 84000, "state": "submitted", "aksatax_id": RETUR_ID})
        amounts = credit._coretax_retur_amounts(faktur=faktur)
        self.assertEqual((amounts["dpp"], amounts["ppn"]), (800000, 96000))

    def test_cancel_and_replace_blocked_by_live_retur(self):
        retur = self._out_retur(state="approved", aksatax_id=RETUR_ID)
        faktur = retur.faktur_id
        message = "Faktur ini punya nota retur yang masih berlaku. Batalkan nota retur dulu."
        for call in (faktur.action_open_cancel_wizard, faktur.action_request_replacement,
                     faktur.move_id.action_coretax_cancel_efaktur,
                     faktur.move_id.action_coretax_request_replacement):
            with self.assertRaisesRegex(UserError, message):
                call()
        retur.state = "cancelled"
        faktur.action_open_cancel_wizard()

    def test_referensi_in_payload(self):
        retur = self._in_retur(referensi="REF-123")
        self.assertEqual(retur._payload()["referensi"], "REF-123")
        retur.referensi = "x" * 150
        self.assertEqual(len(retur._payload()["referensi"]), 100)

    def test_wizard_stores_referensi(self):
        credit = self._in_credit()
        _fake, patcher = self._patch(lookup_routes({("POST", "/nota-retur"): (201, retur_body())}))
        with patcher:
            self._wizard(credit).action_confirm()
        self.assertEqual(credit.coretax_retur_ids.referensi, credit.name)

    def test_refresh_not_found_sets_error(self):
        retur = self._out_retur(state="draft", aksatax_id=RETUR_ID)
        _fake, patcher = self._patch({
            ("GET", f"/nota-retur/{RETUR_ID}"): (404, {"error": {"code": "NOT_FOUND", "message": "Nota retur tidak ada"}})})
        with patcher:
            retur.action_refresh()
        self.assertEqual(retur.state, "error")
        self.assertFalse(retur.aksatax_id)
        self.assertEqual(retur.last_error, "Nota retur tidak ditemukan di PajakMu. Kirim ulang dari Odoo.")

    def test_refresh_pending_covers_draft_and_not_found(self):
        retur = self._out_retur(state="draft", aksatax_id=RETUR_ID,
                                last_sync=fields.Datetime.now() - timedelta(hours=1))
        _fake, patcher = self._patch({
            ("GET", f"/nota-retur/{RETUR_ID}"): (404, {"error": {"code": "NOT_FOUND", "message": "tidak ada"}}),
            ("GET", "/events"): (200, {"data": [], "next_after": 0, "has_more": False}),
        })
        with patcher:
            self.connection._sync()
        self.assertEqual(retur.state, "error")
        self.assertFalse(retur.aksatax_id)

    def test_cancel_rejected_posts_chatter(self):
        retur = self._out_retur(state="cancel_requested", aksatax_id=RETUR_ID)
        retur.write({"state": "approved", "last_error": "DJP menolak"})
        self.assertIn("Pembatalan nota retur RTR/001 tidak berhasil: DJP menolak. Nota retur tetap berlaku.",
                      retur.move_id.message_ids[0].body)

    def test_views_not_creatable(self):
        for view_type in ("list", "form"):
            arch = self.env["coretax.retur"].get_view(view_type=view_type)["arch"]
            self.assertIn('create="0"', arch)

    def test_wizard_reuses_unsent_retur(self):
        credit = self._in_credit()
        old = self.env["coretax.retur"].create({
            "sisi": "masukan", "move_id": credit.id, "nomor_dokumen_retur": "LAMA", "tanggal_retur": "2026-10-05",
            "state": "error", "line_ids": [(0, 0, {"index": 1, "nama": "lama", "retur_dpp": 1})]})
        _fake, patcher = self._patch(lookup_routes({
            ("POST", "/nota-retur"): (201, retur_body()),
            ("POST", f"/nota-retur/{RETUR_ID}/kirim"): (200, retur_body("SUBMITTED"))}))
        with patcher:
            self._wizard(credit, nomor_dokumen_retur="BARU/1").action_confirm()
        self.assertEqual(credit.coretax_retur_ids, old)
        # isi dari wizard menimpa nota retur lama; baris lama diganti baris wizard
        self.assertEqual((old.nomor_dokumen_retur, old.state), ("BARU/1", "submitted"))
        self.assertEqual(old.line_ids.mapped("index"), [0])

    def test_transient_create_error_keeps_version(self):
        for code, status, bumped in (("RETUR_INVALID", 422, True), ("UNAVAILABLE", 503, False)):
            retur = self._in_retur()
            _fake, patcher = self._patch({("POST", "/nota-retur"): (status, {"error": {"code": code, "message": "x"}})})
            with patcher:
                retur.action_send()
            self.assertEqual(retur.state, "error")
            self.assertEqual(retur.version, 2 if bumped else 1, code)
            retur.unlink()

    def test_amounts_zero_and_exempt_lines_count_in_dpp(self):
        for xmlid in ("tax_ST0", "tax_ST2"):
            credit, faktur = self._sale_credit(self._chart_tax(xmlid), faktur_vals={})
            self.assertEqual(credit._coretax_retur_amounts(faktur=faktur),
                             {"dpp": 1000000, "ppn": 0, "ppnbm": 0, "tarif": 12}, xmlid)

    def test_remote_pending_approval_is_draft(self):
        retur = self._in_retur(state="submitted", aksatax_id=RETUR_ID)
        retur._apply_remote(retur_body("PENDING_APPROVAL"))
        self.assertEqual(retur.state, "draft")
