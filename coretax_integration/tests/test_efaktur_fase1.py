from odoo.exceptions import UserError
from odoo.tests import tagged

from ..tools.client import AksaTaxError
from .common import CP_ID, FAKTUR_ID, CoretaxTestCommon, FakeResponse


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
