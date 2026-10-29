import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.client import AksaTaxError

RETUR_STATES = [
    ("new", "Baru"),
    ("draft", "Draft di AksaTax"),
    ("submitted", "Dikirim"),
    ("approved", "Disetujui DJP"),
    ("rejected", "Ditolak DJP"),
    ("cancel_requested", "Proses batal"),
    ("cancelled", "Dibatalkan"),
    ("error", "Gagal - perlu tindakan"),
]
REMOTE_RETUR_STATES = {
    "DRAFT": "draft", "SUBMITTED": "submitted", "APPROVED": "approved", "REJECTED": "rejected",
    "CANCEL_REQUESTED": "cancel_requested", "CANCELLED": "cancelled",
}
# Nota retur yang masih berlaku di AksaTax (menahan faktur kredit Odoo).
LIVE_STATES = ("draft", "submitted", "approved", "cancel_requested")
SENDABLE_STATES = ("new", "draft", "error")
ATTENTION_STATES = ("rejected", "error")


def _digits(value):
    return re.sub(r"\D", "", value or "")


class CoretaxRetur(models.Model):
    """Nota retur (pengembalian BKP/JKP) di AksaTax untuk satu faktur kredit Odoo."""

    _name = "coretax.retur"
    _description = "Nota Retur Coretax"
    _inherit = ["mail.thread"]
    _order = "id desc"
    _check_company_auto = True

    name = fields.Char(string="Nota retur", compute="_compute_name", store=True)
    sisi = fields.Selection([("keluaran", "Keluaran"), ("masukan", "Masukan")], string="Sisi", required=True)
    move_id = fields.Many2one("account.move", string="Faktur kredit Odoo", required=True, ondelete="restrict",
                              index=True, check_company=True)
    origin_move_id = fields.Many2one("account.move", string="Faktur asal", related="move_id.reversed_entry_id",
                                     store=True, index=True)
    faktur_id = fields.Many2one("coretax.faktur", string="e-Faktur asal", index=True, ondelete="restrict",
                                check_company=True)
    nomor_faktur = fields.Char(string="Nomor Faktur Pajak asal")
    partner_id = fields.Many2one("res.partner", string="Lawan transaksi", check_company=True)
    nomor_dokumen_retur = fields.Char(string="Nomor dokumen retur", required=True)
    tanggal_retur = fields.Date(string="Tanggal retur", required=True)
    masa = fields.Integer(string="Masa pajak", compute="_compute_masa_tahun", store=True)
    tahun = fields.Integer(string="Tahun pajak", compute="_compute_masa_tahun", store=True)
    company_id = fields.Many2one(related="move_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="company_id.currency_id", string="Mata uang")
    nilai_dpp = fields.Monetary(string="DPP retur")
    nilai_ppn = fields.Monetary(string="PPN retur")
    nilai_ppnbm = fields.Monetary(string="PPnBM retur")
    tarif_ppn = fields.Float(string="Tarif PPN (%)", default=12)

    state = fields.Selection(RETUR_STATES, string="Status", default="new", required=True, index=True, tracking=True)
    aksatax_id = fields.Char(string="ID AksaTax", readonly=True, copy=False, index=True)
    last_error = fields.Text(string="Keterangan dari AksaTax/DJP", readonly=True, copy=False)
    error_code = fields.Char(string="Kode galat", readonly=True, copy=False)
    version = fields.Integer(string="Versi", default=1, readonly=True, copy=False)
    send_attempt = fields.Integer(string="Percobaan kirim", default=0, readonly=True, copy=False)
    cancel_attempt = fields.Integer(string="Percobaan batal", default=0, readonly=True, copy=False)
    last_sync = fields.Datetime(string="Diperbarui dari AksaTax", readonly=True, copy=False)
    alasan_batal = fields.Char(string="Alasan batal", readonly=True, copy=False)

    @api.depends("nomor_dokumen_retur")
    def _compute_name(self):
        for rec in self:
            rec.name = rec.nomor_dokumen_retur or _("Nota retur")

    @api.depends("tanggal_retur")
    def _compute_masa_tahun(self):
        for rec in self:
            rec.masa = rec.tanggal_retur.month if rec.tanggal_retur else 0
            rec.tahun = rec.tanggal_retur.year if rec.tanggal_retur else 0

    def _idempotency_base(self):
        uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid", "odoo")
        return f"odoo-{uuid}-retur-{self.id}-v{self.version}"

    # ── kirim ke AksaTax ──────────────────────────────────────────────
    def _payload(self):
        self.ensure_one()
        payload = {
            "sisi": self.sisi.upper(),
            "nomorDokumenRetur": self.nomor_dokumen_retur,
            "tanggalRetur": fields.Date.to_string(self.tanggal_retur),
            "nilaiReturDpp": int(round(self.nilai_dpp)),
            "nilaiReturPpn": int(round(self.nilai_ppn)),
            "nilaiReturPpnbm": int(round(self.nilai_ppnbm)),
            "tarifPpn": self.tarif_ppn,
            "referensi": (self.move_id.ref or self.move_id.name or self.nomor_dokumen_retur or "")[:100],
        }
        if self.sisi == "keluaran":
            payload["fakturId"] = self.faktur_id.aksatax_id
        else:
            seller = self.partner_id or self.move_id.commercial_partner_id
            seller = seller.commercial_partner_id
            address = ", ".join(part for part in (seller.street, seller.street2, seller.city, seller.state_id.name,
                                                  seller.zip) if part)
            payload.update({
                "nomorFaktur": _digits(self.nomor_faktur),
                "namaPenjual": seller.name,
                "npwpPenjual": _digits(seller.vat),
                "alamatPenjual": address,
            })
        return payload

    def _check_before_send(self):
        """Daftar masalah (kalimat Indonesia) yang menghalangi pengiriman; kosong = siap."""
        self.ensure_one()
        problems = []
        if not self.nomor_dokumen_retur:
            problems.append(_("Nomor dokumen retur wajib diisi."))
        if self.nilai_dpp <= 0:
            problems.append(_("DPP retur harus lebih dari 0."))
        if self.sisi == "keluaran":
            if not self.faktur_id or not self.faktur_id.aksatax_id:
                problems.append(_("e-Faktur asal belum ada di AksaTax. Kirim dulu e-Faktur-nya."))
            elif self.faktur_id.state != "approved":
                problems.append(_("e-Faktur asal belum disetujui DJP. Nota retur hanya untuk faktur yang sudah disetujui."))
        else:
            seller = (self.partner_id or self.move_id.commercial_partner_id).commercial_partner_id
            if len(_digits(self.nomor_faktur)) not in (13, 16, 17):
                problems.append(_("Nomor Faktur Pajak vendor harus 13, 16, atau 17 digit."))
            if len(_digits(seller.vat)) not in (15, 16):
                problems.append(_("NPWP vendor harus 15 atau 16 digit. Lengkapi NPWP di kontak vendor."))
        return problems

    def _record_error(self, exc, **extra):
        self.write({"state": "error", "last_error": exc.user_message, "error_code": exc.code or False,
                    "last_sync": fields.Datetime.now(), **extra})
        self.message_post(body=_("Gagal mengirim nota retur: %s", exc.user_message))

    def action_send(self):
        for rec in self:
            if rec.state not in SENDABLE_STATES:
                raise UserError(_("Nota retur %s tidak bisa dikirim pada status ini.", rec.name))
            problems = rec._check_before_send()
            if problems:
                raise UserError("\n".join(problems))
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            client = connection._client(rec)
            base = rec._idempotency_base()
            if not rec.aksatax_id:
                try:
                    data = client.create_retur(rec._payload(), base)
                except AksaTaxError as exc:
                    rec._record_error(exc, version=rec.version + 1)
                    continue
                rec.write({"aksatax_id": data.get("id"), "state": "draft", "last_error": False, "error_code": False})
            try:
                result = client.send_retur(rec.aksatax_id, f"{base}-kirim-{rec.send_attempt + 1}")
            except AksaTaxError as exc:
                rec.write({"state": "draft", "last_error": exc.user_message, "error_code": exc.code or False,
                           "last_sync": fields.Datetime.now()})
                rec.message_post(body=_("Nota retur tersimpan sebagai draft di AksaTax, tetapi belum terkirim: %s",
                                        exc.user_message))
                continue
            rec.write({
                "state": REMOTE_RETUR_STATES.get((result.get("status") or "").upper(), "submitted"),
                "send_attempt": rec.send_attempt + 1, "last_error": False, "error_code": False,
                "last_sync": fields.Datetime.now(),
            })
            rec.move_id.message_post(body=_("Nota retur %s dikirim ke DJP.", rec.nomor_dokumen_retur))
        return True

    # ── pembaruan dari AksaTax ────────────────────────────────────────
    def _apply_remote(self, data):
        """Isi dari bentuk `publicRetur` AksaTax."""
        self.ensure_one()
        vals = {"last_sync": fields.Datetime.now()}
        state = REMOTE_RETUR_STATES.get((data.get("status") or "").upper())
        if state:
            vals["state"] = state
        if data.get("id"):
            vals["aksatax_id"] = data["id"]
        if "lastError" in data:
            vals["last_error"] = data.get("lastError") or False
        elif state and state not in ATTENTION_STATES:
            vals["last_error"] = False
        if state and state not in ATTENTION_STATES:
            vals["error_code"] = False
        self.write(vals)

    def _apply_event(self, event, connection):
        """Event retur.* (webhook / polling); data event ringkas, jadi detail diambil ulang."""
        self.ensure_one()
        data = event.get("data") or {}
        state = REMOTE_RETUR_STATES.get((data.get("status") or "").upper()) or {
            "retur.approved": "approved", "retur.rejected": "rejected", "retur.cancelled": "cancelled",
        }.get(event.get("type"))
        vals = {"last_sync": fields.Datetime.now()}
        if state:
            vals["state"] = state
        if data.get("alasan"):
            vals["last_error"] = data["alasan"]
        self.write(vals)
        if state in ("approved", "rejected", "cancelled"):
            try:
                self._apply_remote(connection._client(self).get_retur(self.aksatax_id))
            except AksaTaxError:
                pass  # status sudah tercatat dari event; rincian menyusul saat sinkronisasi berikutnya
        nomor = self.nomor_dokumen_retur
        if state == "approved":
            self.move_id.message_post(body=_("Nota retur %s disetujui DJP.", nomor))
        elif state == "rejected":
            self.move_id.message_post(body=_("Nota retur %s ditolak DJP: %s.", nomor,
                                             self.last_error or _("tanpa keterangan")))
        elif state == "cancelled":
            self.move_id.message_post(body=_("Nota retur %s dibatalkan.", nomor))

    def action_refresh(self):
        for rec in self.filtered("aksatax_id"):
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            try:
                rec._apply_remote(connection._client(rec).get_retur(rec.aksatax_id))
            except AksaTaxError as exc:
                raise UserError(exc.full_message()) from exc
        return True

    def action_open_cancel_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Batalkan nota retur"),
            "res_model": "coretax.retur.cancel.wizard", "view_mode": "form", "target": "new",
            "context": {"default_retur_id": self.id},
        }
