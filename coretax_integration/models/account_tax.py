from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AccountTax(models.Model):
    _inherit = "account.tax"

    coretax_tax_object_id = fields.Many2one(
        "coretax.tax.object", string="Kode objek PPh (Coretax)", index="btree_not_null",
        help="Bila diisi, pajak ini dianggap pajak PPh yang dipotong dari vendor dan menjadi dasar bukti potong.")

    @api.constrains("coretax_tax_object_id", "type_tax_use", "amount_type", "amount")
    def _check_coretax_pph(self):
        for tax in self.filtered("coretax_tax_object_id"):
            if tax.type_tax_use != "purchase" or tax.amount_type != "percent" or tax.amount > 0:
                raise ValidationError(_(
                    "Pajak PPh harus pajak pembelian bertipe persen dengan tarif 0 atau negatif "
                    "(mis. -2 untuk PPh 23 2%)."))

    def _prepare_base_line_for_taxes_computation(self, record, **kwargs):
        # EXTENDS 'account'
        results = super()._prepare_base_line_for_taxes_computation(record, **kwargs)
        results["coretax_tax_object_id"] = self._get_base_line_field_value_from_record(
            record, "coretax_tax_object_id", kwargs, self.env["coretax.tax.object"])
        return results

    def _prepare_tax_line_for_taxes_computation(self, record, **kwargs):
        # EXTENDS 'account'
        results = super()._prepare_tax_line_for_taxes_computation(record, **kwargs)
        results["coretax_tax_object_id"] = self._get_base_line_field_value_from_record(
            record, "coretax_tax_object_id", kwargs, self.env["coretax.tax.object"])
        return results

    def _prepare_base_line_grouping_key(self, base_line):
        # EXTENDS 'account'
        # Wajib di sini, bukan hanya di repartition key: _sync_tax_lines hanya menghitung
        # ulang pajak bila field di key ini berubah.
        results = super()._prepare_base_line_grouping_key(base_line)
        results["coretax_tax_object_id"] = base_line["coretax_tax_object_id"].id
        return results

    def _prepare_base_line_tax_repartition_grouping_key(self, base_line, base_line_grouping_key, tax_data, tax_rep_data):
        # EXTENDS 'account'
        results = super()._prepare_base_line_tax_repartition_grouping_key(
            base_line, base_line_grouping_key, tax_data, tax_rep_data)
        # Hanya journal item PPh yang dipecah per kode objek; PPN tetap satu.
        results["coretax_tax_object_id"] = (
            base_line["coretax_tax_object_id"].id if tax_data["tax"].coretax_tax_object_id else False)
        return results

    def _prepare_tax_line_repartition_grouping_key(self, tax_line):
        # EXTENDS 'account'
        results = super()._prepare_tax_line_repartition_grouping_key(tax_line)
        results["coretax_tax_object_id"] = tax_line["coretax_tax_object_id"].id
        return results
