from . import controllers, models, tools, wizard


def _coretax_post_init(env):
    """Muat pajak PPh untuk perusahaan yang sudah memakai chart template `id`."""
    companies = env["res.company"].search([("chart_template", "=", "id"), ("parent_id", "=", False)],
                                          order="parent_path")
    for company in companies:
        Template = env["account.chart.template"].with_company(company)
        data = {
            "account.tax.group": {
                xmlid: values for xmlid, values in Template._get_id_coretax_account_tax_group().items()
                if not Template.ref(xmlid, raise_if_not_found=False)
            },
            "account.tax": {
                xmlid: values for xmlid, values in Template._get_id_coretax_account_tax().items()
                if not Template.ref(xmlid, raise_if_not_found=False)
            },
        }
        Template._load_data(data)
