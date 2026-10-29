from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.coretax_retur import LIVE_STATES


class CoretaxReturWizard(models.TransientModel):
    """Kirim nota retur dari nota kredit (penjualan atau pembelian) yang sudah diposting."""

    _name = "coretax.retur.wizard"
    _description = "Kirim nota retur"

    move_id = fields.Many2one("account.move", string="Nota kredit", required=True, ondelete="cascade")
    sisi = fields.Selection([("keluaran", "Keluaran"), ("masukan", "Masukan")], compute="_compute_sisi")
    company_id = fields.Many2one(related="move_id.company_id")
    currency_id = fields.Many2one(related="move_id.company_id.currency_id")
    nomor_dokumen_retur = fields.Char(string="Nomor dokumen retur", required=True)
    tanggal_retur = fields.Date(string="Tanggal retur", required=True)
    nilai_dpp = fields.Monetary(string="DPP retur")
    nilai_ppn = fields.Monetary(string="PPN retur")
    nilai_ppnbm = fields.Monetary(string="PPnBM retur")
    tarif_ppn = fields.Float(string="Tarif PPN (%)", default=12)
    referensi = fields.Char(string="Referensi")

    @api.depends("move_id.move_type")
    def _compute_sisi(self):
        for wiz in self:
            wiz.sisi = "masukan" if wiz.move_id.move_type == "in_refund" else "keluaran"

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        move_id = res.get("move_id") or (self.env.context.get("active_id")
                                         if self.env.context.get("active_model") == "account.move" else False)
        move = self.env["account.move"].browse(move_id).exists()
        if move:
            amounts = move._coretax_retur_amounts()
            res.update({
                "move_id": move.id, "nomor_dokumen_retur": move.name,
                "tanggal_retur": move.invoice_date or move.date, "referensi": move.name,
                "nilai_dpp": amounts["dpp"], "nilai_ppn": amounts["ppn"], "nilai_ppnbm": amounts["ppnbm"],
                "tarif_ppn": amounts["tarif"],
            })
        return res

    def _check_preconditions(self):
        move = self.move_id
        if move.move_type not in ("out_refund", "in_refund") or move.state != "posted":
            raise UserError(_("Posting nota kredit dulu, lalu kirim nota retur."))
        origin = move.reversed_entry_id
        if not origin:
            raise UserError(_("Nota kredit ini tidak terhubung ke faktur asal. Buat nota kredit lewat tombol "
                              "Tambah Nota Kredit di faktur asal."))
        live = move.sudo().coretax_retur_ids.filtered(lambda r: r.state in LIVE_STATES)[:1]
        if live:
            raise UserError(_("Nota kredit ini sudah punya nota retur %s. Buka lewat tombol Nota Retur.",
                              live.nomor_dokumen_retur))
        faktur = self.env["coretax.faktur"]
        if move.move_type == "out_refund":
            fakturs = origin.with_context(active_test=False).sudo().coretax_faktur_ids
            faktur = fakturs.filtered(lambda f: f.state == "approved").sorted("version", reverse=True)[:1]
            if not faktur:
                raise UserError(_("Faktur asal belum punya e-Faktur yang disetujui DJP. Kirim e-Faktur faktur "
                                  "asal dulu."))
        elif not origin.coretax_vendor_tax_number:
            raise UserError(_("Isi Nomor Faktur Pajak vendor di tagihan asal (tab Info Lainnya) dulu."))
        if self.nilai_dpp <= 0:
            raise UserError(_("Nilai DPP retur harus lebih dari 0."))
        return faktur

    def action_confirm(self):
        self.ensure_one()
        faktur = self._check_preconditions()
        move = self.move_id
        keluaran = move.move_type == "out_refund"
        retur = self.env["coretax.retur"].create({
            "sisi": "keluaran" if keluaran else "masukan",
            "move_id": move.id,
            "faktur_id": faktur.id if keluaran else False,
            "nomor_faktur": faktur.nomor_faktur if keluaran else move.reversed_entry_id.coretax_vendor_tax_number,
            "partner_id": move.commercial_partner_id.id,
            "nomor_dokumen_retur": self.nomor_dokumen_retur,
            "tanggal_retur": self.tanggal_retur,
            "nilai_dpp": self.nilai_dpp, "nilai_ppn": self.nilai_ppn, "nilai_ppnbm": self.nilai_ppnbm,
            "tarif_ppn": self.tarif_ppn,
        })
        retur.action_send()
        return {
            "type": "ir.actions.act_window", "res_model": "coretax.retur", "res_id": retur.id,
            "view_mode": "form", "views": [[False, "form"]],
        }
