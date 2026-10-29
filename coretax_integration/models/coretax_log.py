from datetime import timedelta

from odoo import api, fields, models

LOG_KEEP_DAYS = 90


class CoretaxLog(models.Model):
    """Riwayat Sinkronisasi: satu baris per panggilan ke AksaTax (dan per
    webhook yang diterima). Isi API key tidak pernah dicatat."""

    _name = "coretax.log"
    _description = "Riwayat Sinkronisasi AksaTax"
    _order = "id desc"
    _rec_name = "operation"

    connection_id = fields.Many2one("coretax.connection", string="Koneksi", ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", string="Perusahaan", index=True)
    operation = fields.Char(string="Kegiatan", required=True)
    success = fields.Boolean(string="Berhasil")
    method = fields.Char(string="Metode")
    path = fields.Char(string="Alamat")
    status_code = fields.Integer(string="Kode HTTP")
    request_id = fields.Char(string="ID Permintaan", help="X-Request-Id dari AksaTax - sebutkan saat menghubungi dukungan AksaTax.")
    duration_ms = fields.Integer(string="Durasi (ms)")
    message = fields.Text(string="Pesan")
    technical = fields.Text(string="Rincian teknis")
    payload = fields.Text(string="Data dikirim")
    response = fields.Text(string="Jawaban AksaTax")
    res_model = fields.Char(string="Model dokumen")
    res_id = fields.Integer(string="ID dokumen")
    document_name = fields.Char(string="Dokumen")

    def action_open_document(self):
        self.ensure_one()
        if not self.res_model or not self.res_id or self.res_model not in self.env:
            return False
        return {
            "type": "ir.actions.act_window", "res_model": self.res_model, "res_id": self.res_id,
            "views": [[False, "form"]], "target": "current",
        }

    @api.autovacuum
    def _gc_old_logs(self):
        self.sudo().search([("create_date", "<", fields.Datetime.now() - timedelta(days=LOG_KEEP_DAYS))]).unlink()
