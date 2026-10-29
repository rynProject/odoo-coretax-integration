from unittest.mock import patch

from freezegun import freeze_time

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import CP_ID, FAKTUR_ID, CoretaxTestCommon, faktur_body


@tagged("post_install", "-at_install", "post_install_l10n")
class TestCoretaxFlow(CoretaxTestCommon):

    def test_payload_uses_odoo_efaktur_lines(self):
        move = self._invoice()
        payload = move._coretax_payload(CP_ID)
        self.assertEqual(payload["counterpartyId"], CP_ID)
        self.assertEqual(payload["kdJenisTransaksi"], "04")
        self.assertEqual(payload["tanggalFaktur"], "2026-10-01")
        self.assertEqual(payload["referensi"], move.name)
        [item] = payload["items"]
        self.assertEqual(item["hargaSatuan"], 50000.0)
        self.assertEqual(item["jumlahBarang"], 2.0)
        self.assertEqual(item["diskon"], 10000.0, "diskon AksaTax dalam rupiah, bukan persen")
        self.assertEqual(len(item["kodeObjek"]), 6)

    def test_send_flow_auto_matches_partner_and_submits(self):
        move = self._invoice()
        self.assertTrue(move.coretax_can_send)
        fake, patcher = self._patch({
            ("GET", "/lawan-transaksi"): (200, {"data": [{"id": CP_ID, "jenisId": "NPWP", "npwp": "1234567890123457",
                                                         "nama": "PT Pelanggan", "vswpStatus": "AKTIF"}]}),
            ("POST", "/faktur"): (201, faktur_body()),
            ("POST", f"/faktur/{FAKTUR_ID}/kirim"): (202, {"id": FAKTUR_ID, "status": "SUBMITTED", "attempt": 1}),
        })
        with patcher:
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
        self.assertEqual(faktur.state, "submitted")
        self.assertEqual(faktur.aksatax_id, FAKTUR_ID)
        self.assertEqual(move.coretax_state, "submitted")
        self.assertEqual(self.partner_a.with_company(move.company_id).coretax_counterparty_id, CP_ID)
        post_calls = [c for c in fake.calls if c["method"] == "POST"]
        self.assertTrue(all(c["headers"].get("Idempotency-Key") for c in post_calls))
        self.assertTrue(fake.calls[0]["headers"]["Authorization"].startswith("Bearer ak_test_"))
        self.assertFalse(move.coretax_can_send, "faktur yang sedang diproses tidak dikirim lagi")
        with self.assertRaises(UserError):
            move.button_draft()
        self.assertTrue(self.env["coretax.log"].search_count([("connection_id", "=", self.connection.id)]) >= 3)

    def test_event_approved_updates_invoice(self):
        move = self._invoice()
        self.partner_a.with_company(move.company_id).coretax_counterparty_id = CP_ID
        fake, patcher = self._patch({
            ("POST", "/faktur"): (201, faktur_body()),
            ("POST", f"/faktur/{FAKTUR_ID}/kirim"): (202, {"id": FAKTUR_ID, "status": "SUBMITTED"}),
            ("GET", f"/faktur/{FAKTUR_ID}"): (200, faktur_body("APPROVED", nomorFaktur="0400260000000001",
                                                               kodeApproval="APPR-1", tanggalApproval="2026-10-01T03:00:00Z")),
            ("GET", "/events"): (200, {"data": [
                {"id": "evt_7", "sequence": 7, "type": "faktur.approved", "object": {"type": "faktur", "id": FAKTUR_ID},
                 "data": {"id": FAKTUR_ID, "nomor": "0400260000000001", "status": "APPROVED"}},
                {"id": "evt_8", "sequence": 8, "type": "faktur.approved", "object": {"type": "faktur", "id": "lain"},
                 "data": {"id": "lain", "status": "APPROVED"}},
            ], "next_after": 8, "has_more": False}),
        })
        with patcher:
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
            summary = self.connection._sync()
        self.assertEqual(summary["events"], 2)
        self.assertEqual(self.connection.event_cursor, 8)
        self.assertEqual(faktur.state, "approved")
        self.assertEqual(faktur.nomor_faktur, "0400260000000001")
        self.assertEqual(faktur.kode_approval, "APPR-1")
        self.assertEqual(move.coretax_nomor_faktur, "0400260000000001")

    def test_validation_error_is_readable(self):
        move = self._invoice()
        self.partner_a.with_company(move.company_id).coretax_counterparty_id = CP_ID
        _fake, patcher = self._patch({
            ("POST", "/faktur"): (422, {"error": {"code": "VALIDATION", "message": "Data tidak valid",
                                                  "details": [{"path": ["items", 0, "kodeObjek"], "message": "Kode objek wajib 6 digit"}]}}),
        })
        with patcher:
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
        self.assertEqual(faktur.state, "error")
        self.assertTrue(faktur.attention)
        self.assertIn("Kode objek wajib 6 digit", faktur.last_error)
        self.assertTrue(move.coretax_can_send, "faktur yang gagal boleh dikirim ulang")

    def test_unknown_customer_gets_instructions_when_auto_register_off(self):
        self.connection.auto_register_partner = False
        move = self._invoice()
        _fake, patcher = self._patch({("GET", "/lawan-transaksi"): (200, {"data": []})})
        with patcher:
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
        self.assertEqual(faktur.state, "error")
        self.assertIn("Lawan Transaksi", faktur.last_error)

    def test_check_blocks_unsupported_transaction_code(self):
        move = self._invoice()
        move.l10n_id_kode_transaksi = "10"
        with patch("odoo.addons.coretax_integration.models.account_move.SUPPORTED_TRX_CODES",
                   {"01", "02", "03", "04", "05", "06", "07", "08", "09"}):
            levels = [level for level, _msg in move._coretax_check(self.connection)]
            self.assertIn("error", levels)
            with self.assertRaises(UserError):
                move._coretax_create_faktur(connection=self.connection)

    def test_send_wizard_reports_results(self):
        move = self._invoice()
        self.partner_a.with_company(move.company_id).coretax_counterparty_id = CP_ID
        wizard = self.env["coretax.send.wizard"].with_context(default_move_ids=[(6, 0, move.ids)]).create({})
        self.assertEqual(wizard.line_ids.level, "ok")
        _fake, patcher = self._patch({
            ("POST", "/faktur"): (201, faktur_body()),
            ("POST", f"/faktur/{FAKTUR_ID}/kirim"): (202, {"id": FAKTUR_ID, "status": "SUBMITTED"}),
        })
        with patcher:
            wizard.action_send()
        self.assertEqual(wizard.step, "done")
        self.assertEqual(wizard.line_ids.result, "sent")
        self.assertEqual(wizard.sent_count, 1)

    def test_bupot_draft(self):
        self.partner_a.with_company(self.company_data["company"]).coretax_counterparty_id = CP_ID
        bupot = self.env["coretax.bupot"].create({
            "partner_id": self.partner_a.id, "tax_object_id": self.env.ref("coretax_integration.tax_object_24_104_01").id,
            "jumlah_bruto": 10_000_000, "dok_nomor": "INV/V/001",
        })
        self.assertEqual(bupot.pph_estimasi, 200_000)
        fake, patcher = self._patch({
            ("POST", "/bupot-unifikasi"): (201, {"id": "33333333-3333-4333-8333-333333333333", "status": "DRAFT"}),
        })
        with patcher:
            bupot.action_send_draft()
        self.assertEqual(bupot.state, "draft")
        sent = fake.calls[-1]["json"]
        self.assertEqual(sent["kodeObjek"], "24-104-01")
        self.assertEqual(sent["jumlahBruto"], 10_000_000)
        self.assertEqual(sent["dokumenDasar"][0]["jenisDok"], "02")

    def test_bulk_partner_match_and_rejection_activity(self):
        move = self._invoice()
        fake, patcher = self._patch({
            ("GET", "/lawan-transaksi"): (200, {"data": [{"id": CP_ID, "jenisId": "NPWP", "npwp": "1234567890123457",
                                                         "nama": "PT Pelanggan", "vswpStatus": "AKTIF"}]}),
            ("POST", "/faktur"): (201, faktur_body()),
            ("POST", f"/faktur/{FAKTUR_ID}/kirim"): (202, {"id": FAKTUR_ID, "status": "SUBMITTED"}),
            ("GET", f"/faktur/{FAKTUR_ID}"): (200, faktur_body("REJECTED", lastError="NPWP pembeli tidak aktif")),
        })
        with patcher:
            self.env["res.partner"].action_coretax_match_all()
            self.assertEqual(self.partner_a.with_company(move.company_id).coretax_counterparty_id, CP_ID)
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
            faktur._apply_event({"type": "faktur.rejected", "object": {"type": "faktur", "id": FAKTUR_ID},
                                 "data": {"id": FAKTUR_ID, "status": "REJECTED", "alasan": "NPWP pembeli tidak aktif"}},
                                self.connection)
        self.assertEqual(faktur.state, "rejected")
        self.assertTrue(faktur.attention)
        activity = move.activity_ids.filtered(lambda a: "Perbaiki e-Faktur" in (a.summary or ""))
        self.assertTrue(activity, "penolakan harus memunculkan aktivitas untuk penanggung jawab faktur")
        self.assertTrue(move.coretax_can_send)

    def test_dashboard_data(self):
        self._invoice()
        data = self.env["coretax.faktur"].get_dashboard_data()
        self.assertEqual(data["counts"]["ready"], 1)
        self.assertEqual(data["connection"]["environment"], "sandbox")

    def _faktur(self, invoice_date, **vals):
        return self.env["coretax.faktur"].create({"move_id": self._invoice(invoice_date=invoice_date).id, **vals})

    @freeze_time("2026-10-25")
    def test_dashboard_month_summary_counts_only_approved_this_month(self):
        self._faktur("2026-10-01", state="approved", total_dpp=100000, total_ppn=11000)
        self._faktur("2026-10-05", state="rejected", total_dpp=50000, total_ppn=5500)
        self._faktur("2026-09-30", state="approved", total_dpp=70000, total_ppn=7700)
        month = self.env["coretax.faktur"].get_dashboard_data()["month"]
        self.assertEqual(month["start"], "2026-10-01")
        self.assertEqual(month["approved"], 1)
        self.assertEqual(month["dpp"], 100000)
        self.assertEqual(month["ppn"], 11000)
        self.assertEqual({row["state"]: row["count"] for row in month["by_state"]}, {"approved": 1, "rejected": 1})

    def test_dashboard_splits_attention_into_rejected_and_failed(self):
        self._faktur("2026-10-01", state="rejected", last_error="NPWP tidak aktif")
        self._faktur("2026-10-02", state="error", last_error="Pelanggan belum terdaftar")
        # draft yang gagal dikirim ke DJP juga termasuk "gagal dikirim"
        self._faktur("2026-10-03", state="draft", send_mode="send", last_error="AksaTax tidak menjawab")
        counts = self.env["coretax.faktur"].get_dashboard_data()["counts"]
        self.assertEqual(counts["attention"], 3)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(counts["failed"], 2)
