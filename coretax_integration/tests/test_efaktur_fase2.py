from odoo.tests import tagged

from .common import CP_ID, CoretaxTestCommon
from .test_efaktur_fase1 import TestEfakturFase1


@tagged("post_install", "-at_install", "post_install_l10n")
class TestEfakturFase2(CoretaxTestCommon):

    _zero_tax = TestEfakturFase1._zero_tax
    _invoice_code = TestEfakturFase1._invoice_code
    _errors = TestEfakturFase1._errors

    def _invoice_10(self):
        # Odoo melarang pajak 0% pada kode 10, jadi pakai faktur dengan PPN biasa.
        move = self._invoice()
        move.l10n_id_kode_transaksi = "10"
        return move

    def test_code_10_sends(self):
        move = self._invoice_10()
        self.assertEqual(self._errors(move), [])
        payload = move._coretax_payload(CP_ID)
        self.assertEqual(payload["kdJenisTransaksi"], "10")
        self.assertNotIn("idKeteranganTambahan", payload)

    def test_code_10_ignores_stale_keterangan(self):
        move = self._invoice_code("07", "TD.00505")
        move.l10n_id_kode_transaksi = "10"
        self.assertNotIn("idKeteranganTambahan", move._coretax_payload(CP_ID))

    def _send_rejected(self, path, move=None, details=None):
        move = move or self._invoice_10()
        if details is None:
            details = [{"path": path, "message": "Invalid option"}]
        self.partner_a.with_company(move.company_id).coretax_counterparty_id = CP_ID
        _fake, patcher = self._patch({("POST", "/faktur"): (422, {"error": {
            "code": "VALIDATION", "message": "Data tidak valid", "details": details}})})
        with patcher:
            return move._coretax_create_faktur(connection=self.connection)

    def test_code_10_on_old_aksatax(self):
        faktur = self._send_rejected("kdJenisTransaksi")
        self.assertEqual(faktur.state, "error")
        self.assertEqual(faktur.error_code, "NOT_SUPPORTED")
        self.assertIn("Kode transaksi 10 butuh AksaTax versi terbaru. Minta admin AksaTax memperbarui server.",
                      faktur.last_error)

    def test_code_10_other_validation_unchanged(self):
        faktur = self._send_rejected("items.0.kodeObjek")
        self.assertEqual(faktur.state, "error")
        self.assertEqual(faktur.error_code, "VALIDATION")

    def test_non_dict_detail_does_not_crash(self):
        faktur = self._send_rejected(None, details=["oops"])
        self.assertEqual(faktur.state, "error")
        self.assertEqual(faktur.error_code, "VALIDATION")

    def test_new_keterangan_on_old_aksatax(self):
        move = self._invoice_code("07", "TD.00505")
        faktur = self._send_rejected("idKeteranganTambahan", move=move)
        self.assertEqual(faktur.state, "error")
        self.assertEqual(faktur.error_code, "NOT_SUPPORTED")
        self.assertIn("Keterangan tambahan ini butuh AksaTax versi terbaru. Minta admin AksaTax memperbarui server.",
                      faktur.last_error)

    def test_code_10_list_form_path(self):
        faktur = self._send_rejected(["kdJenisTransaksi"])
        self.assertEqual(faktur.state, "error")
        self.assertEqual(faktur.error_code, "NOT_SUPPORTED")
