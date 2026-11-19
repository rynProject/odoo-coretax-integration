from odoo import fields, models


class AccountTaxTemplate(models.Model):
    _inherit = "account.tax.template"

    coretax_tax_object_id = fields.Many2one(
        "coretax.tax.object", string="Kode objek PPh (Coretax)",
        help="Diteruskan ke pajak yang dibuat dari template ini.")

    def _get_tax_vals(self, company, tax_template_to_tax):
        vals = super()._get_tax_vals(company, tax_template_to_tax)
        if self.coretax_tax_object_id:
            vals["coretax_tax_object_id"] = self.coretax_tax_object_id.id
        return vals
