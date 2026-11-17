from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.client import AksaTaxError

RETUR_STATES = [
    ("new", "Baru"),
    ("draft", "Draft di PajakMu"),
    ("submitted", "Dikirim"),
    ("approved", "Disetujui DJP"),
    ("rejected", "Ditolak DJP"),
    ("cancel_requested", "Proses batal"),
    ("cancelled", "Dibatalkan"),
    ("error", "Gagal - perlu tindakan"),
]
REMOTE_RETUR_STATES = {
    # Menunggu persetujuan / siap kirim di PajakMu: masih draft dari sisi Odoo (belum terkirim ke DJP).
    "DRAFT": "draft", "PENDING_APPROVAL": "draft", "VALIDATED": "draft", "SUBMITTED": "submitted", "APPROVED": "approved", "REJECTED": "rejected",
    "CANCEL_REQUESTED": "cancel_requested", "CANCELLED": "cancelled",
}
# Nota retur yang masih berlaku di PajakMu (menahan faktur kredit Odoo).
LIVE_STATES = ("draft", "submitted", "approved", "cancel_requested")
SENDABLE_STATES = ("new", "draft", "error")
ATTENTION_STATES = ("rejected", "error")
# Galat sementara: kiriman belum tentu diterima PajakMu, jadi percobaan ulang memakai kunci idempotensi yang sama.
TRANSIENT_CODES = ("TIMEOUT", "CONNECTION", "UNAVAILABLE", "RATE_LIMITED", "IDEMPOTENCY_IN_PROGRESS")
NOT_FOUND_MESSAGE = "Nota retur tidak ditemukan di PajakMu. Kirim ulang dari Odoo."
# Aturan PajakMu/Coretax: retur atas faktur keluaran dibuat pembeli di Coretax, bukan oleh penjual.
KELUARAN_MESSAGE = ("Retur atas faktur keluaran dibuat oleh pembeli di Coretax, bukan dari Odoo. "
                    "Tarik returnya dari DJP di PajakMu › Retur pajak.")
# Nilai retur per barang/jasa: field Odoo -> field PajakMu (`items[]`) dan field faktur asal / sudah diretur.
ITEM_VALUES = [
    ("retur_jumlah", "returJmlBrgJasa", "jmlBrgJasa"),
    ("retur_diskon", "returDiskon", "diskon"),
    ("retur_dpp", "returDpp", "dpp"),
    ("retur_dpp_lain", "returDppLain", "dppLain"),
    ("retur_ppn", "returPpn", "ppn"),
    ("retur_ppnbm", "returPpnbm", "ppnbm"),
]


class CoretaxRetur(models.Model):
    """Nota retur (pengembalian BKP/JKP) di PajakMu untuk satu faktur kredit Odoo.

    Retur baru hanya untuk sisi masukan (nota kredit vendor) atas faktur masukan
    yang sudah ada di PajakMu, per barang/jasa. Retur keluaran lama tetap
    disinkronkan dan bisa dibatalkan."""

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
    referensi = fields.Char(string="Referensi")
    faktur_masukan_id = fields.Char(string="ID faktur masukan PajakMu", copy=False,
                                    help="Faktur masukan di PajakMu yang diretur (hasil tarikan dari DJP).")
    is_creditable = fields.Boolean(string="Retur dapat dikreditkan", default=True)
    line_ids = fields.One2many("coretax.retur.line", "retur_id", string="Barang/jasa diretur", copy=True)

    state = fields.Selection(RETUR_STATES, string="Status", default="new", required=True, index=True, tracking=True)
    aksatax_id = fields.Char(string="ID PajakMu", readonly=True, copy=False, index=True)
    last_error = fields.Text(string="Keterangan dari PajakMu/DJP", readonly=True, copy=False)
    error_code = fields.Char(string="Kode galat", readonly=True, copy=False)
    version = fields.Integer(string="Versi", default=1, readonly=True, copy=False)
    send_attempt = fields.Integer(string="Percobaan kirim", default=0, readonly=True, copy=False)
    cancel_attempt = fields.Integer(string="Percobaan batal", default=0, readonly=True, copy=False)
    last_sync = fields.Datetime(string="Diperbarui dari PajakMu", readonly=True, copy=False)
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

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.filtered("line_ids")._sync_totals_from_lines()
        return records

    def _sync_totals_from_lines(self):
        for rec in self:
            lines = rec.line_ids
            super(CoretaxRetur, rec).write({
                "nilai_dpp": sum(lines.mapped("retur_dpp")), "nilai_ppn": sum(lines.mapped("retur_ppn")),
                "nilai_ppnbm": sum(lines.mapped("retur_ppnbm")),
            })

    def write(self, vals):
        if "line_ids" in vals:
            result = self._write_state(vals)
            self.filtered("line_ids")._sync_totals_from_lines()
            return result
        return self._write_state(vals)

    def _write_state(self, vals):
        # Kembali ke "approved" dari "cancel_requested" = DJP menolak pembatalan.
        cancel_rejected = (self.filtered(lambda r: r.state == "cancel_requested")
                           if vals.get("state") == "approved" else self.browse())
        result = super().write(vals)
        for rec in cancel_rejected:
            rec.move_id.message_post(body=_(
                "Pembatalan nota retur %(nomor)s tidak berhasil: %(alasan)s. Nota retur tetap berlaku.",
                nomor=rec.nomor_dokumen_retur, alasan=rec.last_error or _("tanpa keterangan")))
        return result

    # ── kirim ke PajakMu ──────────────────────────────────────────────
    def _payload(self):
        """Isi POST /nota-retur: retur faktur masukan per barang/jasa (IF_TXR_017)."""
        self.ensure_one()
        if self.sisi == "keluaran":
            raise UserError(_(KELUARAN_MESSAGE))
        payload = {
            "sisi": "MASUKAN",
            "fakturMasukanId": self.faktur_masukan_id,
            "tanggalRetur": fields.Date.to_string(self.tanggal_retur),
            "isCreditable": bool(self.is_creditable),
            "items": [line._payload() for line in self.line_ids.sorted("index") if line._has_value()],
            "referensi": (self.referensi or self.move_id.name or self.nomor_dokumen_retur or "")[:100],
        }
        if self.nomor_dokumen_retur:
            payload["nomorDokumenRetur"] = self.nomor_dokumen_retur[:50]
        return payload

    def _check_before_send(self):
        """Daftar masalah (kalimat Indonesia) yang menghalangi pengiriman; kosong = siap."""
        self.ensure_one()
        if self.sisi == "keluaran":
            return [_(KELUARAN_MESSAGE)]
        problems = []
        if not self.faktur_masukan_id:
            problems.append(_("Faktur masukan asal belum dipilih. Kirim nota retur lewat tombol Kirim nota retur "
                              "di nota kredit vendor."))
        if not any(line.retur_dpp > 0 for line in self.line_ids):
            problems.append(_("DPP retur harus lebih dari 0 pada minimal satu barang/jasa."))
        indexes = self.line_ids.mapped("index")
        if len(indexes) != len(set(indexes)):
            problems.append(_("Barang/jasa yang sama tercantum dua kali."))
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
                    # galat sementara: kiriman belum tentu gagal di PajakMu, jadi kunci idempotensi dipakai ulang
                    rec._record_error(exc, **({} if exc.code in TRANSIENT_CODES else {"version": rec.version + 1}))
                    continue
                rec.write({"aksatax_id": data.get("id"), "state": "draft", "last_error": False, "error_code": False})
            try:
                result = client.send_retur(rec.aksatax_id, f"{base}-kirim-{rec.send_attempt + 1}")
            except AksaTaxError as exc:
                rec.write({"state": "draft", "last_error": exc.user_message, "error_code": exc.code or False,
                           "last_sync": fields.Datetime.now()})
                rec.message_post(body=_("Nota retur tersimpan sebagai draft di PajakMu, tetapi belum terkirim: %s",
                                        exc.user_message))
                continue
            rec.write({
                "state": REMOTE_RETUR_STATES.get((result.get("status") or "").upper(), "submitted"),
                "send_attempt": rec.send_attempt + 1, "last_error": False, "error_code": False,
                "last_sync": fields.Datetime.now(),
            })
            rec.move_id.message_post(body=_("Nota retur %s dikirim ke DJP.", rec.nomor_dokumen_retur))
        return True

    # ── pembaruan dari PajakMu ────────────────────────────────────────
    def _apply_remote(self, data):
        """Isi dari bentuk `publicRetur` PajakMu."""
        self.ensure_one()
        vals = {"last_sync": fields.Datetime.now()}
        state = REMOTE_RETUR_STATES.get((data.get("status") or "").upper())
        if state:
            vals["state"] = state
        if data.get("id"):
            vals["aksatax_id"] = data["id"]
        if data.get("nomorDokumenRetur"):
            vals["nomor_dokumen_retur"] = data["nomorDokumenRetur"]
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

    def _mark_not_found(self):
        self.ensure_one()
        self.write({"state": "error", "last_error": NOT_FOUND_MESSAGE, "error_code": "NOT_FOUND",
                    "aksatax_id": False, "last_sync": fields.Datetime.now()})

    def action_refresh(self):
        for rec in self.filtered("aksatax_id"):
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            try:
                rec._apply_remote(connection._client(rec).get_retur(rec.aksatax_id))
            except AksaTaxError as exc:
                if exc.code == "NOT_FOUND":
                    rec._mark_not_found()
                    continue
                raise UserError(exc.full_message()) from exc
        return True

    def action_open_cancel_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Batalkan nota retur"),
            "res_model": "coretax.retur.cancel.wizard", "view_mode": "form", "target": "new",
            "context": {"default_retur_id": self.id},
        }


class CoretaxReturLine(models.Model):
    """Nilai retur satu barang/jasa faktur masukan asal (urutan `index` dari DJP)."""

    _name = "coretax.retur.line"
    _description = "Barang/jasa nota retur"
    _order = "index"

    retur_id = fields.Many2one("coretax.retur", required=True, ondelete="cascade", index=True)
    index = fields.Integer(string="No. barang/jasa", required=True, help="Urutan pada faktur asal di DJP, mulai 0.")
    nama = fields.Char(string="Barang/jasa")
    retur_jumlah = fields.Float(string="Jumlah", digits=(16, 2))
    retur_diskon = fields.Float(string="Diskon", digits=(16, 2))
    retur_dpp = fields.Float(string="DPP", digits=(16, 2))
    retur_dpp_lain = fields.Float(string="DPP nilai lain", digits=(16, 2))
    retur_ppn = fields.Float(string="PPN", digits=(16, 2))
    retur_ppnbm = fields.Float(string="PPnBM", digits=(16, 2))

    def _has_value(self):
        return any(self[field] for field, _key, _asal in ITEM_VALUES)

    def _payload(self):
        self.ensure_one()
        return {"index": self.index, **{key: round(self[field], 2) for field, key, _asal in ITEM_VALUES}}
