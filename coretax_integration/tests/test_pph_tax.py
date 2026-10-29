from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.coretax_integration import _coretax_post_init

from .common import PphTestCommon


@tagged("post_install", "-at_install", "post_install_l10n")
class TestPphTax(PphTestCommon):

    def test_template_pph_taxes(self):
        expected = [
            (self.pph23, -2.0, "24-104-01", "21210030"),
            (self.pph23_skb, 0.0, "24-104-01", "21210030"),
            (self.pph42, -10.0, "28-402-01", "21210050"),
            (self.env["account.chart.template"].ref("tax_pph26_20"), -20.0, "27-104-01", "21210060"),
        ]
        for tax, amount, code, account in expected:
            self.assertEqual(tax.type_tax_use, "purchase")
            self.assertEqual(tax.amount, amount)
            self.assertEqual(tax.coretax_tax_object_id.code, code)
            self.assertEqual(tax.tax_group_id.name, "PPh")
            reps = tax.invoice_repartition_line_ids | tax.refund_repartition_line_ids
            tax_reps = reps.filtered(lambda r: r.repartition_type == "tax")
            self.assertEqual(set(tax_reps.mapped("account_id.code")), {account})
            self.assertFalse(any(tax_reps.mapped("use_in_tax_closing")))

    def test_pph_tax_must_be_negative_purchase_percent(self):
        base = {"name": "PPh salah", "amount_type": "percent", "coretax_tax_object_id": self.obj_teknik.id}
        with self.assertRaises(ValidationError):
            self.env["account.tax"].create({**base, "amount": 2, "type_tax_use": "purchase"})
        with self.assertRaises(ValidationError):
            self.env["account.tax"].create({**base, "amount": -2, "type_tax_use": "sale"})

    def test_post_init_hook_restores_missing_taxes(self):
        template = self.env["account.chart.template"]
        template.ref("tax_pph26_20").unlink()
        _coretax_post_init(self.env)
        _coretax_post_init(self.env)
        tax = template.ref("tax_pph26_20")
        self.assertEqual(tax.amount, -20.0)
        self.assertEqual(self.env["account.tax"].search_count(
            [("name", "=", tax.name), ("company_id", "=", self.env.company.id)]), 1)
