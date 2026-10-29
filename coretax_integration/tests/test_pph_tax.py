from odoo.exceptions import ValidationError
from odoo.tests import Form, tagged

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

    def test_line_object_defaults_from_tax(self):
        bill = self._bill([(1_000_000, self.pph42), (1_000_000, self.ppn)], post=False)
        self.assertEqual(bill.invoice_line_ids[0].coretax_tax_object_id, self.obj_sewa)
        self.assertFalse(bill.invoice_line_ids[1].coretax_tax_object_id)

    def test_case_three_lines_gl(self):
        bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23), (20_000_000, self.pph42)])
        pph = bill.line_ids.filtered("tax_line_id").sorted("balance")
        self.assertRecordValues(pph, [
            {"tax_line_id": self.pph42.id, "coretax_tax_object_id": self.obj_sewa.id, "balance": -2_000_000},
            {"tax_line_id": self.pph23.id, "coretax_tax_object_id": self.obj_teknik.id, "balance": -300_000},
        ])
        self.assertEqual(pph.mapped("account_id.code"), ["21210050", "21210030"])
        self.assertEqual(bill.amount_total, 32_700_000)

    def test_changed_object_splits_tax_line(self):
        bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23, self.obj_konsultan),
                           (20_000_000, self.pph42)])
        pph23 = bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)
        self.assertEqual(sorted(zip(pph23.mapped("coretax_tax_object_id.code"), pph23.mapped("balance"))),
                         [("24-104-01", -200_000), ("24-104-03", -100_000)])

    def test_ppn_not_split(self):
        bill = self._bill([(10_000_000, self.ppn | self.pph23),
                           (5_000_000, self.ppn | self.pph23, self.obj_konsultan)])
        ppn = bill.line_ids.filtered(lambda l: l.tax_line_id == self.ppn)
        self.assertEqual(len(ppn), 1)
        self.assertFalse(ppn.coretax_tax_object_id)
        self.assertAlmostEqual(ppn.balance, 15_000_000 * self.ppn.amount / 100)
        pph = bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)
        self.assertEqual(sum(pph.mapped("balance")), -300_000, "DPP PPh tidak termasuk PPN")

    def test_write_object_on_draft_resyncs_tax_lines(self):
        bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23)], post=False)
        self.assertEqual(len(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)), 1)
        bill.invoice_line_ids[1].coretax_tax_object_id = self.obj_konsultan
        self.assertEqual(len(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)), 2)

    def test_two_pph_taxes_on_one_line_rejected(self):
        with self.assertRaises(ValidationError):
            self._bill([(1_000_000, self.pph23 | self.pph42)], post=False)

    def test_pph_line_requires_object(self):
        bill = self._bill([(1_000_000, self.pph23)], post=False)
        with self.assertRaisesRegex(ValidationError, "wajib punya kode objek pajak"):
            bill.invoice_line_ids[0].coretax_tax_object_id = False

    def test_early_payment_discount_with_split_pph(self):
        term = self.env["account.payment.term"].create({
            "name": "2/10 net 30 PPh", "early_pay_discount_computation": "included",
            "early_discount": True, "discount_percentage": 2, "discount_days": 10,
            "line_ids": [(0, 0, {"value": "percent", "value_amount": 100, "nb_days": 30})],
        })
        bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23, self.obj_konsultan)],
                          invoice_payment_term_id=term.id)
        term_line = bill.line_ids.filtered(lambda l: l.display_type == "payment_term")
        res = self.env["account.move"]._get_invoice_counterpart_amls_for_early_payment_discount(
            [{"aml": term_line, "amount_currency": term_line.amount_currency, "balance": term_line.balance}],
            term_line.balance)
        tax_vals = [v for v in res["tax_lines"] if "PPh 23" in v["name"]]
        self.assertEqual(len(tax_vals), 1)
        self.assertAlmostEqual(abs(tax_vals[0]["balance"]), 6_000)

    def test_bill_form_fills_object_and_buttons(self):
        with Form(self.env["account.move"].with_context(default_move_type="in_invoice")) as form:
            form.partner_id = self.partner_a
            form.invoice_date = "2026-10-01"
            with form.invoice_line_ids.new() as line:
                line.product_id = self.product_a
                line.price_unit = 20_000_000
                line.tax_ids.clear()
                line.tax_ids.add(self.pph42)
                self.assertEqual(line.coretax_tax_object_id, self.obj_sewa)
        bill = form.save()
        bill.action_post()
        self.assertTrue(bill.coretax_has_pph)
        self.assertEqual(bill.coretax_bupot_count, 1)
        action = bill.action_coretax_view_bupot()
        self.assertEqual(action["res_id"], bill.coretax_bupot_ids.id)

    def test_pph_tax_forbidden_inside_group_tax(self):
        group = self.env["account.tax"].create({
            "name": "Grup", "amount_type": "group", "type_tax_use": "purchase",
            "company_id": self.company_data["company"].id, "children_tax_ids": [(6, 0, [self.ppn.id])]})
        with self.assertRaisesRegex(ValidationError, "grup"):
            group.children_tax_ids = [(4, self.pph23.id)]
        with self.assertRaisesRegex(ValidationError, "grup"):
            self.env["account.tax"].create({
                "name": "Grup2", "amount_type": "group", "type_tax_use": "purchase",
                "company_id": self.company_data["company"].id,
                "children_tax_ids": [(6, 0, [self.ppn.id, self.pph23.id])]})
        plain = self.pph23.copy({"name": "PPh polos", "coretax_tax_object_id": False})
        group2 = self.env["account.tax"].create({
            "name": "Grup3", "amount_type": "group", "type_tax_use": "purchase",
            "company_id": self.company_data["company"].id, "children_tax_ids": [(6, 0, [plain.id])]})
        with self.assertRaisesRegex(ValidationError, "grup"):
            plain.coretax_tax_object_id = self.obj_teknik
