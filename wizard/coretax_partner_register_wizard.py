import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.client import AksaTaxError

_logger = logging.getLogger(__name__)


class CoretaxPartnerRegisterWizard(models.TransientModel):
    """Dua langkah: (1) periksa kelengkapan data setiap kontak; (2) daftarkan
    yang siap ke Lawan Transaksi AksaTax dan tampilkan hasilnya."""

    _name = "coretax.partner.register.wizard"
    _description = "Daftarkan kontak ke AksaTax"

    partner_ids = fields.Many2many("res.partner", string="Kontak dipilih")
    step = fields.Selection([("check", "Periksa"), ("done", "Selesai")], default="check")
    line_ids = fields.One2many("coretax.partner.register.line", "wizard_id", string="Kontak")
    ready_count = fields.Integer(compute="_compute_summary")
    blocked_count = fields.Integer(compute="_compute_summary")
    matched_count = fields.Integer(compute="_compute_summary")
    registered_count = fields.Integer(compute="_compute_summary")
    failed_count = fields.Integer(compute="_compute_summary")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        partner_ids = []
        for command in res.get("partner_ids") or self.env.context.get("default_partner_ids") or []:
            if isinstance(command, (list, tuple)) and command and command[0] == 6:
                partner_ids = command[2]
            elif isinstance(command, int):
                partner_ids.append(command)
        partners = self.env["res.partner"].browse(partner_ids).commercial_partner_id
        connection = self.env["coretax.connection"]._for_company(self.env.company, raise_if_missing=False)
        lines = []
        for partner in partners:
            if partner.coretax_counterparty_id:
                level, messages = "matched", _("Sudah tertaut ke %s di AksaTax.",
                                               partner.coretax_counterparty_name or partner.coretax_counterparty_id)
            else:
                problems = partner._coretax_register_check(connection)
                level = "error" if problems else "ok"
                messages = "\n".join(f"• {p}" for p in problems) or _("Siap didaftarkan.")
            lines.append((0, 0, {"partner_id": partner.id, "level": level, "messages": messages,
                                 "identity": partner._coretax_identity(connection)}))
        res["line_ids"] = lines
        return res

    @api.depends("line_ids.level", "line_ids.result")
    def _compute_summary(self):
        for wiz in self:
            wiz.ready_count = len(wiz.line_ids.filtered(lambda l: l.level == "ok"))
            wiz.blocked_count = len(wiz.line_ids.filtered(lambda l: l.level == "error"))
            wiz.matched_count = len(wiz.line_ids.filtered(lambda l: l.level == "matched"))
            wiz.registered_count = len(wiz.line_ids.filtered(lambda l: l.result in ("created", "linked")))
            wiz.failed_count = len(wiz.line_ids.filtered(lambda l: l.result == "failed"))

    def _reopen(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form",
                "target": "new", "name": _("Daftarkan ke AksaTax")}

    def action_register(self):
        self.ensure_one()
        connection = self.env["coretax.connection"]._for_company(self.env.company)
        if not connection.has_api_key:
            raise UserError(_("API key AksaTax belum diisi. Buka Coretax › Pengaturan › Koneksi AksaTax."))
        ready = self.line_ids.filtered(lambda l: l.level == "ok")
        if not ready:
            raise UserError(_("Tidak ada kontak yang siap didaftarkan. Lengkapi dulu datanya."))
        labels = {"created": _("Lawan transaksi baru dibuat di AksaTax."),
                  "linked": _("NPWP sudah terdaftar; ditautkan ke lawan transaksi yang ada.")}
        for line in ready:
            try:
                with self.env.cr.savepoint():
                    result = line.partner_id.with_company(self.env.company)._coretax_register(connection)
            except AksaTaxError as exc:
                line.write({"result": "failed", "result_message": exc.full_message()})
                continue
            except Exception as exc:  # noqa: BLE001 - satu kontak gagal tidak menghentikan yang lain
                _logger.exception("Daftarkan lawan transaksi gagal untuk %s", line.partner_id.display_name)
                line.write({"result": "failed", "result_message": _("Kesalahan tak terduga: %s", exc)})
                continue
            line.write({"result": result, "result_message": labels[result]})
        (self.line_ids - ready).write({"result": "skipped"})
        for line in self.line_ids.filtered(lambda l: l.result == "skipped"):
            line.result_message = line.messages if line.level == "error" else _("Sudah tertaut, tidak didaftarkan ulang.")
        self.step = "done"
        return self._reopen()


class CoretaxPartnerRegisterLine(models.TransientModel):
    _name = "coretax.partner.register.line"
    _description = "Baris pendaftaran lawan transaksi"

    wizard_id = fields.Many2one("coretax.partner.register.wizard", required=True, ondelete="cascade")
    partner_id = fields.Many2one("res.partner", string="Kontak", required=True)
    identity = fields.Char(string="NPWP/NIK")
    level = fields.Selection([("ok", "Siap"), ("matched", "Sudah tertaut"), ("error", "Data belum lengkap")],
                             string="Pemeriksaan", required=True, default="ok")
    messages = fields.Text(string="Catatan")
    result = fields.Selection([("created", "Didaftarkan"), ("linked", "Ditautkan"), ("failed", "Gagal"),
                               ("skipped", "Dilewati")], string="Hasil")
    result_message = fields.Text(string="Keterangan hasil")

    def action_open_partner(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "res.partner", "res_id": self.partner_id.id,
                "views": [[False, "form"]], "target": "current"}
