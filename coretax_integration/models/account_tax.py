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
