from odoo.tests import tagged

from .common import CoretaxTestCommon


@tagged("post_install", "-at_install", "post_install_l10n")
class TestKesiapanData(CoretaxTestCommon):

    def _counts(self):
        return self.env["coretax.faktur"].get_dashboard_data()["counts"]

    def test_dashboard_counts_products_with_generic_code(self):
        before = self._counts()["products_generic"]
        product = self.env["product.product"].create({"name": "Jasa Baru", "type": "service", "sale_ok": True})
        self.assertEqual(product.l10n_id_product_code.code, "000000", "Odoo memberi kode umum secara bawaan")
        self.assertEqual(self._counts()["products_generic"], before + 1)
        product.l10n_id_product_code = self.env["l10n_id_efaktur_coretax.product.code"].search(
            [("code", "=", "150300")], limit=1)
        self.assertEqual(self._counts()["products_generic"], before)

    def test_dashboard_counts_uoms_without_coretax_code(self):
        before = self._counts()["uoms_no_code"]
        rim = self.env["uom.uom"].create({"name": "Rim", "relative_factor": 1.0,
                                          "relative_uom_id": self.env.ref("uom.product_uom_unit").id})
        self.env["product.product"].create({"name": "Kertas", "sale_ok": True, "uom_id": rim.id})
        self.assertEqual(self._counts()["uoms_no_code"], before + 1)

    def test_dashboard_counts_ready_customers_without_identity(self):
        self._invoice()  # partner_a punya NPWP
        tanpa_npwp = self.env["res.partner"].create({"name": "Toko Tanpa NPWP", "country_id": self.env.ref("base.id").id})
        self._invoice(partner_id=tanpa_npwp.id)
        self.assertEqual(self._counts()["partners_no_id"], 1)
