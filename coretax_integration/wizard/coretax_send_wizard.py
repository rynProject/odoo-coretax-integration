import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.coretax_faktur import STATES

_logger = logging.getLogger(__name__)


class CoretaxSendWizard(models.TransientModel):
    """Dua langkah: (1) periksa setiap faktur - siap / ada catatan / belum
    bisa dikirim; (2) kirim yang siap dan tampilkan hasilnya."""

    _name = "coretax.send.wizard"
    _description = "Kirim e-Faktur ke Coretax"

    move_ids = fields.Many2many("account.move", string="Faktur dipilih")
    step = fields.Selection([("check", "Periksa"), ("done", "Selesai")], default="check")
    mode = fields.Selection([
        ("send", "Langsung kirim ke DJP"),
        ("draft", "Simpan sebagai draft di AksaTax (disetujui/dikirim dari AksaTax)"),
    ], string="Cara kirim", required=True, default="send")
    line_ids = fields.One2many("coretax.send.wizard.line", "wizard_id", string="Faktur")
    connection_ready = fields.Boolean(compute="_compute_summary")
    connection_message = fields.Char(compute="_compute_summary")
    ready_count = fields.Integer(compute="_compute_summary")
    warning_count = fields.Integer(compute="_compute_summary")
    blocked_count = fields.Integer(compute="_compute_summary")
    sent_count = fields.Integer(compute="_compute_summary")
    failed_count = fields.Integer(compute="_compute_summary")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        move_ids = []
        for command in res.get("move_ids") or self.env.context.get("default_move_ids") or []:
            if isinstance(command, (list, tuple)) and command and command[0] == 6:
                move_ids = command[2]
            elif isinstance(command, int):
                move_ids.append(command)
        moves = self.env["account.move"].browse(move_ids)
        connection = self.env["coretax.connection"]._for_company(self.env.company, raise_if_missing=False)
        if connection:
            res["mode"] = connection.default_send_mode
        lines = []
        for move in moves:
            issues = move._coretax_check(connection)
            level = "error" if any(l == "error" for l, _m in issues) else "warning" if issues else "ok"
            lines.append((0, 0, {
                "move_id": move.id, "level": level,
                "messages": "\n".join(f"• {m}" for _l, m in issues) or _("Siap dikirim."),
            }))
        res["line_ids"] = lines
        return res

    @api.depends("line_ids.level", "line_ids.result")
    def _compute_summary(self):
        for wiz in self:
            connection = self.env["coretax.connection"]._for_company(self.env.company, raise_if_missing=False)
            wiz.connection_ready = bool(connection and connection.has_api_key)
            if not connection:
                wiz.connection_message = _("Koneksi AksaTax belum diatur.")
            elif not connection.has_api_key:
                wiz.connection_message = _("API key AksaTax belum diisi.")
            elif connection.state == "error":
                wiz.connection_message = _("Uji koneksi terakhir gagal - periksa Pengaturan › Koneksi AksaTax.")
            else:
                wiz.connection_message = False
            wiz.ready_count = len(wiz.line_ids.filtered(lambda l: l.level == "ok"))
            wiz.warning_count = len(wiz.line_ids.filtered(lambda l: l.level == "warning"))
            wiz.blocked_count = len(wiz.line_ids.filtered(lambda l: l.level == "error"))
            wiz.sent_count = len(wiz.line_ids.filtered(lambda l: l.result in ("sent", "draft")))
            wiz.failed_count = len(wiz.line_ids.filtered(lambda l: l.result == "failed"))

    def _reopen(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form",
                "target": "new", "name": _("Kirim e-Faktur ke Coretax")}

    def action_send(self):
        self.ensure_one()
        connection = self.env["coretax.connection"]._for_company(self.env.company)
        if not connection.has_api_key:
            raise UserError(_("API key AksaTax belum diisi. Buka Coretax › Pengaturan › Koneksi AksaTax."))
        sendable = self.line_ids.filtered(lambda l: l.level != "error")
        if not sendable:
            raise UserError(_("Tidak ada faktur yang siap dikirim. Perbaiki dulu catatan pada setiap faktur."))
        labels = dict(STATES)
        for line in sendable:
            try:
                with self.env.cr.savepoint():
                    faktur = line.move_id._coretax_create_faktur(mode=self.mode, connection=connection)
            except UserError as exc:
                line.write({"result": "failed", "result_message": str(exc.args[0] if exc.args else exc)})
                continue
            except Exception as exc:  # noqa: BLE001 - satu faktur gagal tidak menghentikan yang lain
                _logger.exception("Kirim e-Faktur gagal untuk %s", line.move_id.name)
                line.write({"result": "failed", "result_message": _("Kesalahan tak terduga: %s", exc)})
                continue
            failed = faktur.state == "error" or (faktur.last_error and faktur.state in ("draft", "validated")
                                                 and self.mode == "send")
            line.write({
                "faktur_id": faktur.id,
                "result": "failed" if failed else ("sent" if self.mode == "send" else "draft"),
                "result_message": faktur.last_error if failed else labels.get(faktur.state),
                "needs_match": failed and faktur.error_code == "COUNTERPARTY_MISSING",
            })
        self.line_ids.filtered(lambda l: l.level == "error").write({"result": "skipped",
                                                                     "result_message": _("Dilewati - belum lengkap.")})
        self.step = "done"
        return self._reopen()

    def action_open_connection(self):
        return self.env["coretax.connection"].action_open_for_company()

    def action_view_results(self):
        fakturs = self.line_ids.faktur_id
        action = self.env["ir.actions.act_window"]._for_xml_id("coretax_integration.action_coretax_faktur")
        action["domain"] = [("id", "in", fakturs.ids)]
        return action


class CoretaxSendWizardLine(models.TransientModel):
    _name = "coretax.send.wizard.line"
    _description = "Baris pemeriksaan e-Faktur"

    wizard_id = fields.Many2one("coretax.send.wizard", required=True, ondelete="cascade")
    move_id = fields.Many2one("account.move", string="Faktur", required=True)
    partner_id = fields.Many2one(related="move_id.commercial_partner_id", string="Pelanggan")
    invoice_date = fields.Date(related="move_id.invoice_date", string="Tanggal")
    amount_total = fields.Monetary(related="move_id.amount_total", string="Total")
    currency_id = fields.Many2one(related="move_id.currency_id")
    level = fields.Selection([("ok", "Siap"), ("warning", "Siap, ada catatan"), ("error", "Belum bisa dikirim")],
                             string="Pemeriksaan", required=True, default="ok")
    messages = fields.Text(string="Catatan")
    result = fields.Selection([("sent", "Terkirim ke DJP"), ("draft", "Tersimpan di AksaTax"),
                               ("failed", "Gagal"), ("skipped", "Dilewati")], string="Hasil")
    result_message = fields.Text(string="Keterangan hasil")
    faktur_id = fields.Many2one("coretax.faktur", string="e-Faktur")
    needs_match = fields.Boolean(string="Pelanggan perlu dicocokkan")

    def action_match_partner(self):
        self.ensure_one()
        return self.move_id.commercial_partner_id.action_coretax_match()

    def action_open_move(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "account.move", "res_id": self.move_id.id,
                "views": [[False, "form"]], "target": "current"}
