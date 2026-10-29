from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.res_partner import digits
from ..tools.client import AksaTaxError


class CoretaxPartnerMatchWizard(models.TransientModel):
    """Cari pelanggan/vendor di daftar Lawan Transaksi AksaTax lalu pilih
    yang sesuai, atau daftarkan sebagai lawan transaksi baru bila belum ada."""

    _name = "coretax.partner.match.wizard"
    _description = "Cocokkan kontak dengan AksaTax"

    partner_id = fields.Many2one("res.partner", string="Kontak", required=True)
    partner_identity = fields.Char(string="NPWP/NIK di Odoo", compute="_compute_partner_info")
    current_counterparty = fields.Char(string="Saat ini tertaut ke", compute="_compute_partner_info")
    search_term = fields.Char(string="Cari (NPWP atau nama)")
    line_ids = fields.One2many("coretax.partner.match.line", "wizard_id", string="Hasil pencarian")
    message = fields.Text(readonly=True)
    searched = fields.Boolean()
    aksatax_url = fields.Char(compute="_compute_partner_info")

    @api.depends("partner_id")
    def _compute_partner_info(self):
        connection = self.env["coretax.connection"]._for_company(self.env.company, raise_if_missing=False)
        for wiz in self:
            wiz.partner_identity = wiz.partner_id._coretax_identity() if wiz.partner_id else False
            wiz.current_counterparty = wiz.partner_id.coretax_counterparty_name or wiz.partner_id.coretax_counterparty_id
            wiz.aksatax_url = f"{connection.base_url.rstrip('/')}/lawan-transaksi" if connection else False

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        partner = self.env["res.partner"].browse(res.get("partner_id") or self.env.context.get("default_partner_id"))
        if partner:
            res["search_term"] = digits(partner.vat) or partner.name
            res.update(self._search_values(partner, res["search_term"]))
        return res

    def _search_values(self, partner, term):
        connection = self.env["coretax.connection"]._for_company(self.env.company, raise_if_missing=False)
        if not connection or not connection.has_api_key:
            return {"message": _("Koneksi AksaTax belum diatur, jadi pencarian belum bisa dilakukan."), "searched": True}
        client = connection._client(partner)
        rows, seen = [], set()
        try:
            term_digits = digits(term)
            queries = []
            if term_digits and len(term_digits) >= 15:
                variants = [term_digits] + (["0" + term_digits] if len(term_digits) == 15 else
                                            [term_digits[1:]] if term_digits.startswith("0") else [])
                queries = [{"npwp": v} for v in variants]
            queries.append({"query": term})
            for query in queries:
                for row in client.find_counterparties(**query):
                    if row.get("id") not in seen:
                        seen.add(row.get("id"))
                        rows.append(row)
                if rows:
                    break
        except AksaTaxError as exc:
            return {"message": exc.full_message(), "searched": True, "line_ids": [(5, 0, 0)]}
        message = False if rows else _(
            "Tidak ditemukan di AksaTax. Klik Daftarkan dari Odoo untuk mendaftarkan kontak ini sebagai "
            "lawan transaksi baru.")
        return {
            "searched": True, "message": message,
            "line_ids": [(5, 0, 0)] + [(0, 0, {
                "counterparty_id": row.get("id"), "nama": row.get("nama"), "npwp": row.get("npwp"),
                "jenis_id": row.get("jenisId"), "vswp_status": row.get("vswpStatus"),
                "same_id": bool(partner._coretax_identity() and digits(row.get("npwp")).lstrip("0")
                                == partner._coretax_identity().lstrip("0")),
            }) for row in rows],
        }

    def action_search(self):
        self.ensure_one()
        self.write(self._search_values(self.partner_id, self.search_term or self.partner_id.name))
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form",
                "target": "new", "name": _("Cocokkan dengan AksaTax")}

    def action_open_aksatax(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.aksatax_url, "target": "new"}

    def action_register(self):
        """Daftarkan kontak ini ke AksaTax sebagai lawan transaksi baru."""
        self.ensure_one()
        connection = self.env["coretax.connection"]._for_company(self.env.company)
        partner = self.partner_id.with_company(self.env.company)
        problems = partner._coretax_register_check(connection)
        if problems:
            raise UserError(_("Data %(partner)s belum lengkap untuk didaftarkan:\n%(problems)s",
                              partner=partner.display_name, problems="\n".join(f"• {p}" for p in problems)))
        try:
            result = partner._coretax_register(connection)
        except AksaTaxError as exc:
            raise UserError(exc.full_message()) from exc
        message = _("%s didaftarkan sebagai lawan transaksi baru di AksaTax.", partner.display_name) \
            if result == "created" else _("NPWP %s sudah terdaftar di AksaTax; kontak ditautkan ke data yang ada.",
                                          partner._coretax_identity(connection))
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Lawan transaksi AksaTax"), "type": "success", "message": message,
                       "next": {"type": "ir.actions.act_window_close"}},
        }


class CoretaxPartnerMatchLine(models.TransientModel):
    _name = "coretax.partner.match.line"
    _description = "Kandidat lawan transaksi AksaTax"

    wizard_id = fields.Many2one("coretax.partner.match.wizard", required=True, ondelete="cascade")
    counterparty_id = fields.Char(string="ID AksaTax", required=True)
    nama = fields.Char(string="Nama di AksaTax")
    npwp = fields.Char(string="NPWP/NIK")
    jenis_id = fields.Char(string="Jenis identitas")
    vswp_status = fields.Char(string="Status VSWP")
    same_id = fields.Boolean(string="NPWP sama")

    def action_choose(self):
        self.ensure_one()
        partner = self.wizard_id.partner_id
        partner._coretax_set_counterparty({"id": self.counterparty_id, "nama": self.nama, "vswpStatus": self.vswp_status})
        partner.message_post(body=_("Dicocokkan dengan lawan transaksi AksaTax: %s (%s).", self.nama, self.npwp or "-"))
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Sudah cocok"), "type": "success",
                       "message": _("%s kini tertaut ke %s di AksaTax.", partner.display_name, self.nama),
                       "next": {"type": "ir.actions.act_window_close"}},
        }
