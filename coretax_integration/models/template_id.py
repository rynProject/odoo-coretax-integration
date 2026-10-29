from odoo import Command, models
from odoo.addons.account.models.chart_template import template

# (xmlid, nama, tarif, kode objek, akun Utang PPh, label faktur)
PPH_TAXES = [
    ("tax_pph23_2", "PPh 23 2%", -2.0, "coretax_integration.tax_object_24_104_01", "l10n_id_21210030", "PPh 23"),
    ("tax_pph23_skb", "PPh 23 SKB 0%", 0.0, "coretax_integration.tax_object_24_104_01", "l10n_id_21210030",
     "PPh 23 SKB"),
    ("tax_pph42_sewa_10", "PPh 4(2) Sewa 10%", -10.0, "coretax_integration.tax_object_28_402_01",
     "l10n_id_21210050", "PPh 4(2)"),
    ("tax_pph26_20", "PPh 26 20%", -20.0, "coretax_integration.tax_object_27_104_01", "l10n_id_21210060", "PPh 26"),
]


class AccountChartTemplate(models.AbstractModel):
    _inherit = "account.chart.template"

    @template("id", "account.tax.group")
    def _get_id_coretax_account_tax_group(self):
        return {
            "l10n_id_tax_group_pph": {"name": "PPh", "country_id": "base.id"},
        }

    @template("id", "account.tax")
    def _get_id_coretax_account_tax(self):
        taxes = {}
        for xmlid, name, amount, tax_object, account, label in PPH_TAXES:
            repartition = []
            for document_type in ("invoice", "refund"):
                repartition += [
                    Command.create({"document_type": document_type, "repartition_type": "base"}),
                    Command.create({"document_type": document_type, "repartition_type": "tax",
                                    "account_id": account, "use_in_tax_closing": False}),
                ]
            taxes[xmlid] = {
                "name": name,
                "amount": amount,
                "amount_type": "percent",
                "type_tax_use": "purchase",
                "tax_group_id": "l10n_id_tax_group_pph",
                "invoice_label": label,
                "coretax_tax_object_id": tax_object,
                "repartition_line_ids": repartition,
            }
        return taxes
