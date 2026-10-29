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
