from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import FAKTUR_ID, CoretaxTestCommon, faktur_body

RETUR_ID = "33333333-3333-4333-8333-333333333333"


def retur_body(status="DRAFT", **extra):
    return {"id": RETUR_ID, "sisi": "KELUARAN", "status": status, "nomorDokumenRetur": "RTR/001",
            "nomorFaktur": "0400260000000001", "tanggalRetur": "2026-10-05", "masaPajakRetur": 10,
            "tahunPajakRetur": 2026, "nilaiReturDpp": 10000, "nilaiReturPpn": 1200, "nilaiReturPpnbm": 0,
            "referensi": "RTR/001", "lastError": None, **extra}


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
            "sisi": "masukan", "move_id": credit.id, "nomor_faktur": bill.coretax_vendor_tax_number,
            "nomor_dokumen_retur": "RTR/002", "tanggal_retur": "2026-10-05",
            "nilai_dpp": 10000, "nilai_ppn": 1200, **vals})

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

    def test_payload_keluaran(self):
        payload = self._out_retur()._payload()
        self.assertEqual(payload["sisi"], "KELUARAN")
        self.assertEqual(payload["fakturId"], FAKTUR_ID)
        self.assertEqual((payload["nilaiReturDpp"], payload["nilaiReturPpn"], payload["nilaiReturPpnbm"]),
                         (10000, 1200, 0))
        self.assertEqual(payload["tanggalRetur"], "2026-10-05")
        self.assertEqual(payload["nomorDokumenRetur"], "RTR/001")
        self.assertEqual(payload["tarifPpn"], 12)

    def test_payload_masukan(self):
        payload = self._in_retur()._payload()
        self.assertEqual(payload["sisi"], "MASUKAN")
        self.assertEqual(payload["nomorFaktur"], "0400022600000099")
        self.assertEqual(payload["npwpPenjual"], "1234567890123457")
        self.assertEqual(payload["namaPenjual"], self.partner_a.commercial_partner_id.name)
        self.assertIn("Jl. Vendor 1", payload["alamatPenjual"])
        self.assertNotIn("fakturId", payload)

    def test_send_success(self):
        retur = self._out_retur()
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
        retur = self._out_retur()
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
        retur = self._out_retur()
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
        retur = self._out_retur()
        _fake, patcher = self._patch({})
        with patcher:
            retur.action_send()
        self.assertEqual(retur.state, "error")
        self.assertIn("butuh AksaTax versi terbaru", retur.last_error)
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

    def test_wizard_defaults(self):
        credit = self._out_credit()
        wizard = self._wizard(credit)
        self.assertEqual(wizard.nomor_dokumen_retur, credit.name)
        self.assertEqual(wizard.tanggal_retur, credit.invoice_date)
        self.assertEqual((wizard.nilai_dpp, wizard.nilai_ppn), (90000, 10800))
        self.assertEqual(wizard.referensi, credit.name)

    def test_wizard_keluaran_sends(self):
        credit = self._out_credit()
        fake, patcher = self._patch({
            ("POST", "/nota-retur"): (201, retur_body()),
            ("POST", f"/nota-retur/{RETUR_ID}/kirim"): (202, {"id": RETUR_ID, "status": "SUBMITTED"}),
        })
        with patcher:
            action = self._wizard(credit).action_confirm()
        retur = self.env["coretax.retur"].browse(action["res_id"])
        self.assertEqual(retur.state, "submitted")
        self.assertEqual(retur.sisi, "keluaran")
        self.assertEqual(fake.calls[0]["json"]["fakturId"], FAKTUR_ID)
        self.assertEqual(credit.coretax_retur_count, 1)

    def test_wizard_masukan_sends(self):
        credit = self._in_credit()
        fake, patcher = self._patch({
            ("POST", "/nota-retur"): (201, retur_body(sisi="MASUKAN")),
            ("POST", f"/nota-retur/{RETUR_ID}/kirim"): (202, {"id": RETUR_ID, "status": "SUBMITTED"}),
        })
        with patcher:
            action = self._wizard(credit).action_confirm()
        retur = self.env["coretax.retur"].browse(action["res_id"])
        self.assertEqual((retur.state, retur.sisi), ("submitted", "masukan"))
        self.assertEqual(fake.calls[0]["json"]["nomorFaktur"], "0400022600000099")
        self.assertEqual(fake.calls[0]["json"]["sisi"], "MASUKAN")

    def test_wizard_requires_posted(self):
        credit = self._out_credit()
        wizard = self._wizard(credit)
        credit.button_draft()
        with self.assertRaisesRegex(UserError, "Posting nota kredit dulu, lalu kirim nota retur"):
            wizard.action_confirm()

    def test_wizard_requires_origin(self):
        credit = self.env["account.move"].create({
            "move_type": "out_refund", "partner_id": self.partner_a.id, "invoice_date": "2026-10-05",
            "invoice_line_ids": [(0, 0, {"product_id": self.product_a.id, "quantity": 1, "price_unit": 1000,
                                         "tax_ids": [(6, 0, self.tax_sale_a.ids)]})],
        })
        credit.action_post()
        with self.assertRaisesRegex(UserError, "tidak terhubung ke faktur asal"):
            self._wizard(credit).action_confirm()

    def test_wizard_rejects_existing_live_retur(self):
        retur = self._out_retur(state="approved", aksatax_id=RETUR_ID)
        with self.assertRaisesRegex(UserError, "sudah punya nota retur RTR/001"):
            self._wizard(retur.move_id).action_confirm()

    def test_wizard_keluaran_needs_approved_faktur(self):
        credit = self._out_credit(approved=False)
        with self.assertRaisesRegex(UserError, "Faktur asal belum punya e-Faktur yang disetujui DJP"):
            self._wizard(credit).action_confirm()

    def test_wizard_masukan_needs_vendor_number(self):
        credit = self._in_credit(vendor_number=False)
        with self.assertRaisesRegex(UserError, "Isi Nomor Faktur Pajak vendor"):
            self._wizard(credit).action_confirm()

    def test_wizard_dpp_must_be_positive(self):
        credit = self._out_credit()
        with self.assertRaisesRegex(UserError, "Nilai DPP retur harus lebih dari 0"):
            self._wizard(credit, nilai_dpp=0).action_confirm()

    def test_credit_note_locked_while_retur_live(self):
        retur = self._out_retur(state="approved", aksatax_id=RETUR_ID)
        credit = retur.move_id
        with self.assertRaisesRegex(UserError, "punya nota retur RTR/001 di AksaTax"):
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
