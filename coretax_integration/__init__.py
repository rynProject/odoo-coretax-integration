from odoo import SUPERUSER_ID, api

from . import controllers, models, tools, wizard


def _coretax_load_pph(env):
    """Buat akun dan pajak PPh dari template modul ini untuk perusahaan yang sudah memakai chart l10n_id."""
    env = env(su=True)
    chart = env.ref("l10n_id.l10n_id_chart", raise_if_not_found=False)
    if not chart:
        return
    ChartTemplate = env["account.chart.template"]
    account_templates = env.ref("coretax_integration.l10n_id_21210060")
    tax_templates = {xmlid: env.ref("coretax_integration.%s" % xmlid)
                     for xmlid in ("tax_pph23_2", "tax_pph23_skb", "tax_pph42_sewa_10", "tax_pph26_20")}
    for company in env["res.company"].search([("chart_template_id", "=", chart.id)]):
        # akun dicari menurut kode (perusahaan bisa sudah membuatnya sendiri), pajak menurut xmlid
        account_vals = [
            (template, chart._get_account_vals(company, template, template.code, {}))
            for template in account_templates
            if not env["account.account"].search_count([("company_id", "=", company.id), ("code", "=", template.code)])
        ]
        if account_vals:
            ChartTemplate._create_records_with_xmlid("account.account", account_vals, company)
        missing = env["account.tax.template"].union(*(
            template for xmlid, template in tax_templates.items()
            if not env.ref("coretax_integration.%s_%s" % (company.id, xmlid), raise_if_not_found=False)))
        if missing:
            missing._generate_tax(company, accounts_exist=True)


def _coretax_post_init(cr, registry):
    _coretax_load_pph(api.Environment(cr, SUPERUSER_ID, {}))
