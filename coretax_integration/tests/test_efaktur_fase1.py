from odoo.exceptions import UserError
from odoo.tests import tagged

from ..tools.client import AksaTaxError
from .common import CP_ID, FAKTUR_ID, CoretaxTestCommon, FakeResponse, faktur_body


@tagged("post_install", "-at_install", "post_install_l10n")
class TestEfakturFase1(CoretaxTestCommon):

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
        fake, patcher = self._patch({})
        with patcher, self.assertRaises(AksaTaxError) as ctx:
            self.connection._client(self.connection).cancel_faktur(FAKTUR_ID, "Salah harga satuan", "k-1")
        self.assertEqual(ctx.exception.code, "NOT_SUPPORTED")
        self.assertIn("AksaTax versi terbaru", ctx.exception.full_message())

    def test_update_counterparty_sends_put(self):
        self.partner_a.coretax_counterparty_id = CP_ID
        fake, patcher = self._patch({("PUT", f"/lawan-transaksi/{CP_ID}"): (200, {
            "id": CP_ID, "jenisId": "NPWP", "npwp": "1234567890123457", "nama": "PT Pelanggan Baru",
            "vswpStatus": "AKTIF"})})
        with patcher:
            self.partner_a.action_coretax_update_counterparty()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["json"], self.partner_a._coretax_counterparty_payload(self.connection))
        self.assertEqual(self.partner_a.coretax_counterparty_name, "PT Pelanggan Baru")
        self.assertIn("diperbarui di AksaTax", " ".join(self.partner_a.message_ids.mapped("body")))

    def test_update_unmatched_partner(self):
        self.partner_a.coretax_counterparty_id = False
        fake, patcher = self._patch({})
        with patcher, self.assertRaisesRegex(UserError, "Cocokkan"):
            self.partner_a.action_coretax_update_counterparty()
        self.assertEqual(fake.calls, [])

    def test_update_counterparty_error_is_readable(self):
        self.partner_a.coretax_counterparty_id = CP_ID
        fake, patcher = self._patch({("PUT", f"/lawan-transaksi/{CP_ID}"): (409, {"error": {
            "code": "COUNTERPARTY_EXISTS", "message": "NPWP sudah dipakai lawan transaksi lain"}})})
        with patcher, self.assertRaisesRegex(UserError, "NPWP sudah dipakai lawan transaksi lain"):
            self.partner_a.action_coretax_update_counterparty()

    # ── kode transaksi 07/08 ──────────────────────────────────────────
    def _zero_tax(self):
        # Kode 07/08 mewajibkan PPN 0%, jadi buat pajak 0% dari grup pajak 0 l10n_id.
        group = self.env["account.chart.template"].with_company(self.company_data["company"]).ref(
            "l10n_id_tax_group_0")
        return self.tax_sale_a.copy({"name": "PPN 0% uji", "amount": 0.0, "tax_group_id": group.id})

    def _invoice_code(self, code, add_info=None):
        move = self._invoice(invoice_line_ids=[(0, 0, {
            "product_id": self.product_a.id, "quantity": 2, "price_unit": 50000,
            "tax_ids": [(6, 0, self._zero_tax().ids)]})])
        move.l10n_id_kode_transaksi = code
        if add_info:
            move["l10n_id_coretax_add_info_%s" % code] = add_info
        return move

    def _errors(self, move):
        return [msg for level, msg in move._coretax_check() if level == "error"]

    def test_code_07_sends_keterangan(self):
        move = self._invoice_code("07", "TD.00505")
        payload = move._coretax_payload(CP_ID)
        self.assertEqual(payload["idKeteranganTambahan"], "5")
        self.assertEqual(payload["kdJenisTransaksi"], "07")
        self.assertEqual(self._errors(move), [])

    def test_code_07_two_digit_keterangan(self):
        move = self._invoice_code("07", "TD.00530")
        self.assertEqual(move._coretax_payload(CP_ID)["idKeteranganTambahan"], "30")

    def test_code_08_sends_keterangan(self):
        move = self._invoice_code("08", "TD.00510")
        payload = move._coretax_payload(CP_ID)
        self.assertEqual(payload["idKeteranganTambahan"], "10")
        self.assertEqual(payload["kdJenisTransaksi"], "08")
        self.assertEqual(self._errors(move), [])

    def test_code_07_requires_keterangan(self):
        move = self._invoice_code("07")
        self.assertTrue(any("keterangan tambahan" in e for e in self._errors(move)))

    def test_code_10_still_unsupported(self):
        move = self._invoice_code("10")
        self.assertTrue(any("belum didukung" in e for e in self._errors(move)))

    def test_code_01_has_no_keterangan(self):
        move = self._invoice_code("01")
        self.assertNotIn("idKeteranganTambahan", move._coretax_payload(CP_ID))

    # ── batalkan e-Faktur ─────────────────────────────────────────────
    def _approved_faktur(self):
        move = self._invoice()
        self.partner_a.with_company(move.company_id).coretax_counterparty_id = CP_ID
        _fake, patcher = self._patch({
            ("POST", "/faktur"): (201, faktur_body()),
            ("POST", f"/faktur/{FAKTUR_ID}/kirim"): (202, {"id": FAKTUR_ID, "status": "SUBMITTED"}),
        })
        with patcher:
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
        faktur.write({"state": "approved", "aksatax_id": FAKTUR_ID})
        return move, faktur

    def _cancel_routes(self):
        return {("POST", f"/faktur/{FAKTUR_ID}/batal"): (202, {"id": FAKTUR_ID, "status": "CANCEL_REQUESTED"})}

    def test_cancel_sends_alasan(self):
        move, faktur = self._approved_faktur()
        fake, patcher = self._patch(self._cancel_routes())
        with patcher:
            wiz = self.env["coretax.faktur.cancel.wizard"].create(
                {"faktur_id": faktur.id, "alasan": "Salah harga satuan"})
            wiz.action_confirm()
        call = fake.calls[0]
        self.assertEqual(call["json"], {"alasan": "Salah harga satuan"})
        self.assertTrue(call["headers"].get("Idempotency-Key"))
        self.assertEqual(faktur.state, "cancel_requested")
        self.assertIn("Salah harga satuan", " ".join(faktur.message_ids.mapped("body")))

    def test_cancelled_event_unlocks_invoice(self):
        move, faktur = self._approved_faktur()
        fake, patcher = self._patch(self._cancel_routes())
        with patcher:
            self.env["coretax.faktur.cancel.wizard"].create(
                {"faktur_id": faktur.id, "alasan": "Salah harga satuan"}).action_confirm()
            self.connection._process_event({
                "id": "evt_90", "sequence": 90, "type": "faktur.cancelled",
                "object": {"type": "faktur", "id": FAKTUR_ID},
                "data": {"id": FAKTUR_ID, "status": "CANCELLED"}})
        self.assertEqual(faktur.state, "cancelled")
        move.button_draft()

    def test_cancel_only_from_approved(self):
        move, faktur = self._approved_faktur()
        faktur.state = "processing"
        fake, patcher = self._patch({})
        with patcher, self.assertRaises(UserError):
            faktur.action_open_cancel_wizard()
        self.assertEqual(fake.calls, [])

    def test_cancel_reason_too_short(self):
        move, faktur = self._approved_faktur()
        fake, patcher = self._patch({})
        with patcher, self.assertRaisesRegex(UserError, "minimal 5 karakter"):
            self.env["coretax.faktur.cancel.wizard"].create(
                {"faktur_id": faktur.id, "alasan": "abc"}).action_confirm()
        self.assertEqual(fake.calls, [])

    def _cancel(self, faktur, alasan):
        self.env["coretax.faktur.cancel.wizard"].create({"faktur_id": faktur.id, "alasan": alasan}).action_confirm()

    def _keys(self, fake):
        return [c["headers"]["Idempotency-Key"] for c in fake.calls if c["path"].endswith("/batal")]

    def _failing_cancel_routes(self):
        return {("POST", f"/faktur/{FAKTUR_ID}/batal"): FakeResponse(
            503, {"error": {"code": "UNAVAILABLE", "message": "Layanan sibuk"}}, headers={"Retry-After": "0"})}

    def test_cancel_twice_uses_new_key(self):
        move, faktur = self._approved_faktur()
        fake, patcher = self._patch(self._cancel_routes())
        with patcher:
            self.assertEqual(faktur.cancel_attempt, 0)
            self._cancel(faktur, "Salah harga satuan")
            self.assertEqual(faktur.cancel_attempt, 1)
            faktur.state = "approved"  # DJP menolak pembatalan
            self._cancel(faktur, "Salah harga satuan")
            self.assertEqual(faktur.cancel_attempt, 2)
        keys = self._keys(fake)
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_failed_cancel_then_changed_alasan_new_key(self):
        move, faktur = self._approved_faktur()
        fake, patcher = self._patch(self._failing_cancel_routes())
        with patcher:
            with self.assertRaises(UserError):
                self._cancel(faktur, "Salah harga satuan")
            self.assertEqual(faktur.cancel_attempt, 0)
            with self.assertRaises(UserError):
                self._cancel(faktur, "Salah jumlah barang")
        keys = self._keys(fake)
        self.assertEqual(len(keys), 4)  # 2 percobaan x 1 ulang internal
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[2], keys[3])
        self.assertNotEqual(keys[0], keys[2])
        self.assertEqual(faktur.cancel_attempt, 0)

    def test_failed_cancel_same_alasan_same_key(self):
        move, faktur = self._approved_faktur()
        fake, patcher = self._patch(self._failing_cancel_routes())
        with patcher:
            for _i in range(2):
                with self.assertRaises(UserError):
                    self._cancel(faktur, "Salah harga satuan")
        keys = self._keys(fake)
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(faktur.cancel_attempt, 0)

    # ── faktur pengganti ──────────────────────────────────────────────
    NEW_ID = "00000000-0000-4000-8000-0000000000b2"
    OLD_NUMBER = "0400260000000001"

    def _approved_with_number(self):
        move, faktur = self._approved_faktur()
        faktur.nomor_faktur = self.OLD_NUMBER
        return move, faktur

    def _resend_routes(self, extra=None):
        return {
            ("POST", "/faktur"): (201, faktur_body(id=self.NEW_ID)),
            ("POST", f"/faktur/{self.NEW_ID}/kirim"): (202, {"id": self.NEW_ID, "status": "SUBMITTED"}),
            **(extra or {}),
        }

    def _event(self, kind, status, seq):
        return {"id": f"evt_{seq}", "sequence": seq, "type": kind,
                "object": {"type": "faktur", "id": self.NEW_ID}, "data": {"id": self.NEW_ID, "status": status}}

    def _faktur_posts(self, fake):
        return [c for c in fake.calls if c["method"] == "POST" and c["path"] == "/faktur"]

    def test_replacement_flow(self):
        move, old = self._approved_with_number()
        old.action_request_replacement()
        self.assertTrue(old.replace_requested)
        move.button_draft()
        move.invoice_line_ids.price_unit = 60000
        move.action_post()
        self.assertTrue(move.coretax_can_send)
        fake, patcher = self._patch(self._resend_routes({("GET", f"/faktur/{self.NEW_ID}"): (200, faktur_body("APPROVED", id=self.NEW_ID,
                                                                    nomorFaktur="0400260000000002"))}))
        with patcher:
            new = move._coretax_create_faktur(mode="send", connection=self.connection)
            payload = self._faktur_posts(fake)[0]["json"]
            self.assertEqual(payload["fgPengganti"], "1")
            self.assertEqual(payload["nomorFakturDiganti"], self.OLD_NUMBER)
            self.assertEqual(new.replaces_id, old)
            self.assertEqual(new.state, "submitted")
            self.assertFalse(move.coretax_can_send)
            self.assertTrue(any("Sudah punya" in e for e in self._errors(move)))
            self.connection._process_event(self._event("faktur.approved", "APPROVED", 100))
        self.assertEqual(old.state, "replaced")
        self.assertEqual(old.replaced_by_id, new)
        self.assertFalse(old.replace_requested)
        self.assertEqual(new.state, "approved")
        self.assertEqual(move.coretax_faktur_id, new)
        self.assertFalse(move.coretax_can_send)
        # event lama untuk e-Faktur yang diganti tidak mengembalikan statusnya
        old._apply_remote(faktur_body("APPROVED"))
        self.assertEqual(old.state, "replaced")
        with patcher:
            self.connection._process_event({
                "id": "evt_101", "sequence": 101, "type": "faktur.approved",
                "object": {"type": "faktur", "id": FAKTUR_ID}, "data": {"id": FAKTUR_ID, "status": "APPROVED"}})
        self.assertEqual(old.state, "replaced")

    def test_cancel_replacement(self):
        move, old = self._approved_with_number()
        with self.assertRaises(UserError):
            move.button_draft()
        old.action_request_replacement()
        old.action_cancel_replacement()
        self.assertFalse(old.replace_requested)
        with self.assertRaises(UserError):
            move.button_draft()
        self.assertFalse(move.coretax_can_send)

    def test_cancel_blocked_while_replacing(self):
        move, old = self._approved_with_number()
        old.action_request_replacement()
        with self.assertRaises(UserError):
            move.button_cancel()

    def test_replacement_rejected_then_resent(self):
        move, old = self._approved_with_number()
        old.action_request_replacement()
        move.button_draft()
        move.action_post()
        fake, patcher = self._patch(self._resend_routes({("GET", f"/faktur/{self.NEW_ID}"): (200, faktur_body("REJECTED", id=self.NEW_ID,
                                                                    lastError="Ditolak"))}))
        with patcher:
            new = move._coretax_create_faktur(mode="send", connection=self.connection)
            self.connection._process_event(self._event("faktur.rejected", "REJECTED", 101))
            self.assertEqual(new.state, "rejected")
            self.assertEqual(old.state, "approved")
            self.assertTrue(move.coretax_can_send)
            newer = move._coretax_create_faktur(mode="send", connection=self.connection)
        posts = self._faktur_posts(fake)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[1]["json"]["fgPengganti"], "1")
        self.assertEqual(posts[1]["json"]["nomorFakturDiganti"], self.OLD_NUMBER)
        self.assertEqual(newer.replaces_id, old)

    def test_request_replacement_only_from_approved(self):
        move, faktur = self._approved_faktur()
        faktur.state = "processing"
        with self.assertRaises(UserError):
            faktur.action_request_replacement()
        faktur.state = "approved"
        faktur.action_request_replacement()
        with self.assertRaises(UserError):
            faktur.action_request_replacement()

    def test_cancel_blocked_after_rejected_replacement(self):
        move, old = self._approved_with_number()
        old.action_request_replacement()
        move.button_draft()
        move.action_post()
        fake, patcher = self._patch(self._resend_routes({("GET", f"/faktur/{self.NEW_ID}"): (200, faktur_body(
            "REJECTED", id=self.NEW_ID, lastError="Ditolak"))}))
        with patcher:
            new = move._coretax_create_faktur(mode="send", connection=self.connection)
            self.connection._process_event(self._event("faktur.rejected", "REJECTED", 102))
        self.assertEqual(new.state, "rejected")
        with self.assertRaises(UserError):
            move.button_cancel()
        with self.assertRaisesRegex(UserError, "kirim ulang faktur pengganti"):
            old.action_cancel_replacement()
        move.button_draft()

    def test_cancel_blocked_after_errored_replacement(self):
        move, old = self._approved_with_number()
        old.action_request_replacement()
        move.button_draft()
        move.action_post()
        fake, patcher = self._patch({("POST", "/faktur"): (422, {"error": {"code": "INVALID", "message": "Data salah"}})})
        with patcher:
            new = move._coretax_create_faktur(mode="send", connection=self.connection)
        self.assertEqual(new.state, "error")
        with self.assertRaises(UserError):
            move.button_cancel()

    def test_old_cancelled_stays_cancelled_when_replacement_approved(self):
        move, old = self._approved_with_number()
        old.action_request_replacement()
        move.button_draft()
        move.action_post()
        fake, patcher = self._patch(self._resend_routes({("GET", f"/faktur/{self.NEW_ID}"): (200, faktur_body(
            "APPROVED", id=self.NEW_ID, nomorFaktur="0400260000000002"))}))
        with patcher:
            new = move._coretax_create_faktur(mode="send", connection=self.connection)
            old.state = "cancelled"
            self.connection._process_event(self._event("faktur.approved", "APPROVED", 103))
        self.assertEqual(new.state, "approved")
        self.assertEqual(old.state, "cancelled")
        self.assertFalse(old.replaced_by_id)

    def test_replacement_needs_old_number(self):
        move, old = self._approved_faktur()
        old.action_request_replacement()
        self.assertTrue(any("Nomor Faktur Pajak e-Faktur lama belum ada" in e for e in self._errors(move)))
        old.nomor_faktur = self.OLD_NUMBER
        self.assertFalse(any("e-Faktur lama belum ada" in e for e in self._errors(move)))

    def test_cancel_clears_replace_requested(self):
        move, old = self._approved_with_number()
        old.action_request_replacement()
        fake, patcher = self._patch(self._cancel_routes())
        with patcher:
            self._cancel(old, "Salah harga satuan")
        self.assertFalse(old.replace_requested)
        self.assertEqual(old.state, "cancel_requested")

    # ── unduh PDF ─────────────────────────────────────────────────────
    def _pdf_route(self, content):
        return {("GET", f"/faktur/{FAKTUR_ID}/pdf"): FakeResponse(
            200, raw=content, headers={"Content-Type": "application/pdf"})}

    def _attachments(self, move):
        return self.env["ir.attachment"].search([("res_model", "=", "account.move"), ("res_id", "=", move.id)])

    def test_download_pdf_creates_single_attachment(self):
        move, faktur = self._approved_faktur()
        faktur.nomor_faktur = "0400260000000001"
        for content in (b"%PDF-1.7 satu", b"%PDF-1.7 dua"):
            _fake, patcher = self._patch(self._pdf_route(content))
            with patcher:
                action = faktur.action_download_pdf()
        att = self._attachments(move)
        self.assertEqual(len(att), 1)
        self.assertEqual(att.name, "Faktur Pajak 0400260000000001.pdf")
        self.assertEqual(att.mimetype, "application/pdf")
        self.assertEqual(att.raw, b"%PDF-1.7 dua")
        self.assertEqual(action["url"], f"/web/content/{att.id}?download=true")

    def test_download_pdf_from_move_uses_latest(self):
        move, faktur = self._approved_faktur()
        faktur.nomor_faktur = "0400260000000001"
        _fake, patcher = self._patch(self._pdf_route(b"%PDF-1.7 x"))
        with patcher:
            action = move.action_coretax_download_pdf()
        self.assertEqual(len(self._attachments(move)), 1)
        self.assertIn("/web/content/", action["url"])

    def test_pdf_requires_approved(self):
        move = self._invoice()
        faktur = self.env["coretax.faktur"].create({"move_id": move.id, "state": "draft", "aksatax_id": FAKTUR_ID})
        fake, patcher = self._patch(self._pdf_route(b"%PDF-1.7 x"))
        with patcher, self.assertRaisesRegex(UserError, "setelah e-Faktur disetujui DJP"):
            faktur.action_download_pdf()
        self.assertEqual(fake.calls, [])

    def test_download_empty_pdf_rejected(self):
        move, faktur = self._approved_faktur()
        faktur.nomor_faktur = "0400260000000001"
        _fake, patcher = self._patch(self._pdf_route(b"%PDF-1.7 lama"))
        with patcher:
            faktur.action_download_pdf()
        _fake, patcher = self._patch(self._pdf_route(b""))
        with patcher, self.assertRaisesRegex(UserError, "kosong"):
            faktur.action_download_pdf()
        att = self._attachments(move)
        self.assertEqual(len(att), 1)
        self.assertEqual(att.raw, b"%PDF-1.7 lama")
