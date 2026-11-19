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

    @api.constrains("coretax_tax_object_id", "children_tax_ids")
    def _check_coretax_pph_not_in_group(self):
        for tax in self:
            pph_children = tax.children_tax_ids.filtered("coretax_tax_object_id")
            pph_self_in_group = tax.coretax_tax_object_id and self.with_context(active_test=False).search_count(
                [("children_tax_ids", "in", tax.ids)])
            if pph_children or pph_self_in_group:
                raise ValidationError(_(
                    "Pajak PPh tidak boleh menjadi bagian dari pajak grup. "
                    "Pasang pajak PPh langsung di baris tagihan, jangan di dalam pajak grup."))
