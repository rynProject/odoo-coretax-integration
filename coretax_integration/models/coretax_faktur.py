import re
from datetime import date, datetime, time, timezone

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.client import AksaTaxError
from .coretax_retur import LIVE_STATES as RETUR_LIVE_STATES

# Status dokumen PajakMu (packages/shared/src/enums.ts) -> status di Odoo.
# "error" khusus Odoo: gagal membuat/mengirim ke PajakMu.
STATES = [
    ("draft", "Draft di PajakMu"),
    ("pending_approval", "Menunggu persetujuan di PajakMu"),
    ("validated", "Siap dikirim ke DJP"),
    ("submitted", "Dikirim ke DJP"),
    ("processing", "Diproses DJP"),
    ("approved", "Disetujui DJP"),
    ("rejected", "Ditolak DJP"),
    ("cancel_requested", "Pembatalan diajukan"),
    ("cancelled", "Dibatalkan"),
    ("replaced", "Diganti"),
    ("error", "Gagal - perlu tindakan"),
]
REMOTE_STATES = {
    "DRAFT": "draft", "PENDING_APPROVAL": "pending_approval", "VALIDATED": "validated",
    "SUBMITTED": "submitted", "PROCESSING": "processing", "APPROVED": "approved",
    "REJECTED": "rejected", "CANCEL_REQUESTED": "cancel_requested", "CANCELLED": "cancelled",
    "AMENDED": "replaced",
}
# Status lapor SPT Masa PPN (turunan PajakMu, field `dilaporkan`).
LAPOR_STATES = [("YA", "Sudah dilaporkan"), ("BELUM", "Belum dilaporkan"), ("PEMBETULAN", "Perlu pembetulan SPT")]
# Status yang masih "hidup" - faktur Odoo yang punya e-Faktur seperti ini tidak
# dikirim ulang dan tidak boleh dikembalikan ke draft. "Menunggu persetujuan" ikut
# mengunci: setelah disetujui, PajakMu mengirim isi lama ke DJP otomatis.
LOCKING_STATES = ("pending_approval", "submitted", "processing", "approved", "cancel_requested")
# Bisa dikirim ke DJP dari Odoo (endpoint /kirim memvalidasi draft dulu).
SENDABLE_STATES = ("draft", "validated")
# Boleh dibuat ulang dari Odoo (dokumen lama diarsipkan).
RECREATE_STATES = ("rejected", "cancelled", "error")
# PDF resmi faktur pajak tersedia setelah disetujui DJP (juga untuk yang sudah diminta batal/diganti).
PDF_STATES = ("approved", "cancel_requested", "cancelled", "replaced")
ATTENTION_STATES = ("rejected", "error")
# Sudah sampai ke DJP: dari "menunggu persetujuan" ke sini berarti disetujui lalu dikirim PajakMu.
SENT_STATES = ("submitted", "processing", "approved", "rejected")
# Peran penyetuju di PajakMu (`persetujuan.peran`), label seperti di aplikasi PajakMu.
APPROVER_ROLES = {"REVIEWER": "Reviewer", "ADMIN_PAJAK": "Admin Pajak", "OWNER": "Owner"}
IN_PROGRESS_STATES = ("pending_approval", "validated", "submitted", "processing", "cancel_requested")
# Kode barang/jasa umum 000000 yang diberikan Odoo ke produk baru (l10n_id_efaktur_coretax).
GENERIC_PRODUCT_CODES = ("l10n_id_efaktur_coretax.product_code_000000_goods",
                         "l10n_id_efaktur_coretax.product_code_000000_service")


def _parse_dt(value):
    """Tanggal/waktu ISO 8601 dari PajakMu -> datetime UTC tanpa tzinfo
    (bentuk simpan Datetime Odoo)."""
    if not value:
        return False
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return datetime.combine(value, time.min)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return False
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class CoretaxFaktur(models.Model):
    """e-Faktur Keluaran di PajakMu untuk satu faktur pelanggan Odoo.

    Setiap pengiriman ulang (setelah ditolak/dibatalkan) membuat dokumen baru
    dengan versi berikutnya; yang lama diarsipkan sebagai riwayat."""

    _name = "coretax.faktur"
    _description = "e-Faktur Coretax"
    _inherit = ["mail.thread"]
    _order = "create_date desc, id desc"
    _check_company_auto = True

    name = fields.Char(string="e-Faktur", compute="_compute_name", store=True)
    active = fields.Boolean(default=True)
    move_id = fields.Many2one("account.move", string="Faktur Odoo", required=True, ondelete="restrict", index=True,
                              check_company=True)
    company_id = fields.Many2one(related="move_id.company_id", store=True, index=True)
    partner_id = fields.Many2one(related="move_id.commercial_partner_id", string="Pelanggan", store=True)
    invoice_date = fields.Date(related="move_id.invoice_date", string="Tanggal faktur", store=True)
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.ref("base.IDR"), readonly=True)
    amount_total = fields.Monetary(related="move_id.amount_total", string="Total faktur Odoo",
                                   currency_field="move_currency_id")
    move_currency_id = fields.Many2one(related="move_id.currency_id", string="Mata uang faktur")

    state = fields.Selection(STATES, string="Status", default="draft", required=True, index=True, tracking=True)
    attention = fields.Boolean(string="Perlu tindakan", compute="_compute_flags", store=True)
    in_progress = fields.Boolean(string="Sedang diproses", compute="_compute_flags", store=True)
    version = fields.Integer(string="Versi", default=1, readonly=True)
    send_mode = fields.Selection([("send", "Kirim ke DJP"), ("draft", "Draft di PajakMu")],
                                 string="Cara kirim", default="send", readonly=True)

    aksatax_id = fields.Char(string="ID PajakMu", readonly=True, copy=False, index=True)
    nomor_faktur = fields.Char(string="Nomor Faktur Pajak", readonly=True, tracking=True, copy=False)
    kode_approval = fields.Char(string="Kode persetujuan DJP", readonly=True, copy=False)
    tanggal_approval = fields.Datetime(string="Disetujui pada", readonly=True, copy=False)
    kd_jenis_transaksi = fields.Char(string="Kode transaksi", readonly=True)
    masa_pajak = fields.Integer(string="Masa pajak", readonly=True)
    tahun_pajak = fields.Integer(string="Tahun pajak", readonly=True)
    total_dpp = fields.Monetary(string="DPP", readonly=True)
    total_dpp_nilai_lain = fields.Monetary(string="DPP nilai lain", readonly=True)
    total_ppn = fields.Monetary(string="PPN", readonly=True)
    total_ppnbm = fields.Monetary(string="PPnBM", readonly=True)
    last_error = fields.Text(string="Keterangan dari PajakMu/DJP", readonly=True)
    error_code = fields.Char(string="Kode galat", readonly=True, copy=False)
    cancel_attempt = fields.Integer(string="Percobaan batal", default=0, readonly=True, copy=False)
    replace_requested = fields.Boolean(string="Penggantian dibuka", default=False, copy=False, tracking=True)
    replaces_id = fields.Many2one("coretax.faktur", string="Mengganti e-Faktur", copy=False, readonly=True,
                                  index=True, ondelete="set null")
    replaced_by_id = fields.Many2one("coretax.faktur", string="Diganti oleh", copy=False, readonly=True,
                                     ondelete="set null")
    send_attempt = fields.Integer(string="Percobaan kirim", default=0, readonly=True, copy=False)
    submitted_at = fields.Datetime(string="Dikirim ke DJP pada", readonly=True, copy=False)
    approval_step = fields.Char(string="Tahap persetujuan", readonly=True, copy=False,
                                help="Langkah persetujuan internal PajakMu yang sedang ditunggu.")
    last_sync = fields.Datetime(string="Diperbarui dari PajakMu", readonly=True, copy=False)
    lapor_state = fields.Selection(LAPOR_STATES, string="Lapor SPT Masa PPN", readonly=True, copy=False,
                                   help="Status lapor dari PajakMu: apakah faktur ini sudah termasuk SPT Masa PPN "
                                        "yang terlapor di Coretax.")
    aksatax_url = fields.Char(string="Tautan PajakMu", compute="_compute_aksatax_url")
    log_count = fields.Integer(compute="_compute_log_count")

    @api.depends("nomor_faktur", "move_id.name", "version")
    def _compute_name(self):
        for rec in self:
            base = rec.nomor_faktur or rec.move_id.name or _("e-Faktur")
            rec.name = base if rec.version <= 1 or rec.nomor_faktur else f"{base} (v{rec.version})"

    @api.depends("state", "last_error", "send_mode")
    def _compute_flags(self):
        for rec in self:
            # draft yang gagal dikirim ke DJP juga perlu tindakan
            rec.attention = rec.state in ATTENTION_STATES or bool(
                rec.state in SENDABLE_STATES and rec.send_mode == "send" and rec.last_error)
            rec.in_progress = rec.state in IN_PROGRESS_STATES

    def write(self, vals):
        state = vals.get("state")
        # Kembali ke "approved" dari "cancel_requested" = DJP menolak pembatalan (PajakMu tidak mengirim event).
        cancel_rejected = self.filtered(lambda r: r.state == "cancel_requested") if state == "approved" else self.browse()
        if state in ("cancel_requested", "cancelled") and "replace_requested" not in vals:
            # Pembatalan (juga dari PajakMu web) menutup penggantian yang masih dibuka.
            vals = {**vals, "replace_requested": False}
        left_approval = self.browse()
        if state and state != "pending_approval":
            left_approval = self.filtered(lambda r: r.state == "pending_approval")
            if "approval_step" not in vals:
                vals = {**vals, "approval_step": False}
        result = super().write(vals)
        for rec in left_approval:
            rec._notify_approval_result()
        if state == "approved":
            for rec in self:
                old = rec.replaces_id
                if old and old.state == "approved":
                    old.write({"state": "replaced", "replaced_by_id": rec.id, "replace_requested": False})
                    rec.move_id.message_post(body=_("e-Faktur %(old)s diganti oleh %(new)s.",
                                                    old=old.nomor_faktur or old.display_name,
                                                    new=rec.nomor_faktur or rec.display_name))
            for rec in cancel_rejected:
                reason = re.sub(r"^Pembatalan ditolak:\s*", "", rec.last_error or "").strip().rstrip(".")
                rec.move_id.message_post(body=_("Pembatalan e-Faktur tidak berhasil: %s. e-Faktur tetap berlaku.",
                                                reason or _("tanpa keterangan")))
        return result

    def _notify_approval_result(self):
        """Catat di faktur Odoo hasil persetujuan internal PajakMu (status baru saja keluar dari
        "Menunggu persetujuan"), dari polling, event, maupun Perbarui status."""
        self.ensure_one()
        if self.state in SENT_STATES:
            if not self.submitted_at:
                self.write({"submitted_at": fields.Datetime.now()})
            body = _("e-Faktur disetujui di PajakMu dan dikirim ke DJP.")
        elif self.state == "validated":
            body = _("e-Faktur disetujui di PajakMu, tetapi belum terkirim ke DJP. "
                     "Klik Kirim ke DJP di e-Faktur untuk mengirimnya.")
        elif self.state == "draft" and self.last_error:
            # PajakMu: lastError "Dikembalikan penyetuju: <alasan>"; awalannya tidak diulang di pesan
            reason = re.sub(r"^Dikembalikan penyetuju:\s*", "", self.last_error).strip().rstrip(".")
            body = _("e-Faktur dikembalikan ke draft oleh penyetuju di PajakMu: %s. "
                     "Perbaiki datanya, lalu kirim lagi ke DJP.", reason or _("tanpa keterangan"))
        elif self.state == "draft":
            body = _("Pengajuan persetujuan e-Faktur ditarik di PajakMu. e-Faktur kembali menjadi draft.")
        else:
            return
        self.move_id.message_post(body=body)

    def _notify_pending_approval(self):
        self.ensure_one()
        step = f" ({self.approval_step})" if self.approval_step else ""
        self.move_id.message_post(body=_(
            "e-Faktur menunggu persetujuan di PajakMu%s. Setelah disetujui, PajakMu mengirimnya ke DJP otomatis.", step))

    def _approval_step_label(self, persetujuan):
        """`persetujuan` PajakMu {langkah, jumlah, peran} -> "Langkah 1 dari 2, menunggu Admin Pajak"."""
        if not isinstance(persetujuan, dict) or not persetujuan.get("langkah"):
            return False
        if persetujuan.get("jumlah"):
            label = _("Langkah %(langkah)s dari %(jumlah)s", langkah=persetujuan["langkah"],
                      jumlah=persetujuan["jumlah"])
        else:
            label = _("Langkah %s", persetujuan["langkah"])
        role = APPROVER_ROLES.get(persetujuan.get("peran"))
        # peran kosong = penyetuju mana pun
        return _("%(langkah)s, menunggu %(peran)s", langkah=label, peran=role) if role else label

    def _compute_aksatax_url(self):
        for rec in self:
            connection = self.env["coretax.connection"]._for_company(rec.company_id, raise_if_missing=False)
            base = (connection.base_url or "").rstrip("/")
            rec.aksatax_url = f"{base}/faktur-keluaran/{rec.aksatax_id}" if base and rec.aksatax_id else False

    def _compute_log_count(self):
        counts = dict(self.env["coretax.log"]._read_group(
            [("res_model", "=", self._name), ("res_id", "in", self.ids)], ["res_id"], ["__count"]))
        for rec in self:
            rec.log_count = counts.get(rec.id, 0)

    def _idempotency_base(self):
        uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid", "odoo")
        return f"odoo-{uuid}-move-{self.move_id.id}-v{self.version}"

    # ── pembaruan dari PajakMu ────────────────────────────────────────
    def _apply_remote(self, data):
        """Isi dokumen dari bentuk `publicFaktur` PajakMu."""
        self.ensure_one()
        vals = {"last_sync": fields.Datetime.now()}
        state = REMOTE_STATES.get((data.get("status") or "").upper())
        if state and not (self.state == "replaced" and state == "approved"):
            vals["state"] = state
        mapping = {
            "id": "aksatax_id", "nomorFaktur": "nomor_faktur", "kodeApproval": "kode_approval",
            "kdJenisTransaksi": "kd_jenis_transaksi", "masaPajak": "masa_pajak", "tahunPajak": "tahun_pajak",
            "totalDpp": "total_dpp", "totalDppNilaiLain": "total_dpp_nilai_lain", "totalPpn": "total_ppn",
            "totalPpnbm": "total_ppnbm",
        }
        for key, field in mapping.items():
            if data.get(key) is not None:
                vals[field] = data[key]
        if "tanggalApproval" in data:
            vals["tanggal_approval"] = _parse_dt(data.get("tanggalApproval"))
        if "dilaporkan" in data:
            vals["lapor_state"] = data.get("dilaporkan") if data.get("dilaporkan") in dict(LAPOR_STATES) else False
        if "lastError" in data:
            vals["last_error"] = data.get("lastError") or False
        if state and state not in ATTENTION_STATES and "lastError" not in data:
            vals["last_error"] = False
        if (state or self.state) == "pending_approval" and "persetujuan" in data:
            vals["approval_step"] = self._approval_step_label(data.get("persetujuan"))
        self.write(vals)

    def _apply_event(self, event, connection):
        """Event faktur.* (webhook / polling). Data event ringkas, jadi untuk
        dokumen yang disetujui detailnya diambil lengkap (nomor, kode DJP)."""
        self.ensure_one()
        data = event.get("data") or {}
        vals = {"last_sync": fields.Datetime.now()}
        if event.get("type") == "faktur.reported":
            # status lapor SPT saja; status dokumen tidak berubah
            lapor = data.get("dilaporkan") or "YA"
            self.write({"last_sync": fields.Datetime.now(),
                        "lapor_state": lapor if lapor in dict(LAPOR_STATES) else False})
            return
        if event.get("type") in ("faktur.approval_rejected", "faktur.approval_withdrawn"):
            self._apply_approval_event(event, connection)
            return
        state = REMOTE_STATES.get((data.get("status") or "").upper()) or {
            "faktur.submitted": "submitted", "faktur.approved": "approved",
            "faktur.rejected": "rejected", "faktur.cancelled": "cancelled", "faktur.amended": "replaced",
        }.get(event.get("type"))
        if event.get("type") == "faktur.amended":
            state = "replaced"
            if self.state == "replaced":
                state = None  # penggantian dari Odoo sudah tercatat
        if self.state == "replaced" and state == "approved":
            state = None  # PajakMu tidak tahu e-Faktur ini sudah diganti; tetap Diganti
        if state:
            vals["state"] = state
        if data.get("nomor"):
            vals["nomor_faktur"] = data["nomor"]
        if data.get("alasan"):
            vals["last_error"] = data["alasan"]
        self.write(vals)
        if state in ("approved", "rejected", "cancelled"):
            try:
                self._apply_remote(connection._client(self).get_faktur(self.aksatax_id))
            except AksaTaxError:
                pass  # status sudah tercatat dari event; rincian menyusul saat sinkronisasi berikutnya
        if state == "rejected":
            self.move_id.message_post(body=_("e-Faktur ditolak DJP: %s", self.last_error or _("tanpa keterangan")))
            self._schedule_fix_activity(_("e-Faktur ditolak DJP"))
        elif state == "approved":
            self.move_id.message_post(body=_("e-Faktur disetujui DJP. Nomor Faktur Pajak: %s", self.nomor_faktur or "-"))
        elif state == "replaced":
            self.move_id.message_post(body=_(
                "e-Faktur %(nomor)s diganti oleh faktur pengganti %(pengganti)s yang dibuat di PajakMu.",
                nomor=self.nomor_faktur or "-", pengganti=data.get("nomorPengganti") or "-"))

    def _apply_approval_event(self, event, connection):
        """Persetujuan internal PajakMu ditolak penyetuju / pengajuannya ditarik: faktur kembali DRAFT.
        Status diambil ulang dari PajakMu (event bisa terlambat, mis. faktur sudah diajukan lagi);
        data event dipakai bila PajakMu tidak bisa dibaca. Pesan di faktur Odoo ditulis oleh write()."""
        self.ensure_one()
        try:
            remote = connection._client(self).get_faktur(self.aksatax_id)
        except AksaTaxError:
            if self.state not in ("pending_approval", "draft"):
                return  # event lama untuk e-Faktur yang sudah dikirim; status menyusul saat sinkronisasi
            data = event.get("data") or {}
            remote = {"status": data.get("status") or "DRAFT", "lastError": None}
            if event.get("type") == "faktur.approval_rejected":
                # bentuk lastError PajakMu untuk penolakan penyetuju
                remote["lastError"] = f"Dikembalikan penyetuju: {data.get('alasan') or ''}".strip()
        self._apply_remote(remote)

    def _schedule_fix_activity(self, summary):
        """Aktivitas 'perbaiki' untuk penanggung jawab faktur, supaya penolakan
        tidak luput (muncul di lonceng aktivitas & chatter faktur)."""
        self.ensure_one()
        move = self.move_id
        user = move.invoice_user_id or move.create_uid
        if not user or user == self.env.ref("base.user_root"):
            return
        note = _("%s<br/>Keterangan: %s<br/>Perbaiki datanya, lalu klik <b>Kirim e-Faktur</b> untuk membuat ulang.",
                 summary, self.last_error or "-")
        move.activity_schedule("mail.mail_activity_data_todo", user_id=user.id,
                               summary=_("Perbaiki e-Faktur %s", move.name), note=note)

    # ── tindakan pengguna ─────────────────────────────────────────────
    def action_send_to_djp(self):
        """Kirim e-Faktur yang masih draft di PajakMu ke DJP."""
        for rec in self:
            if rec.state not in SENDABLE_STATES or not rec.aksatax_id:
                raise UserError(_("e-Faktur %s tidak dalam status yang bisa dikirim ke DJP.", rec.display_name))
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            client = connection._client(rec)
            try:
                result = client.send_faktur(rec.aksatax_id, f"{rec._idempotency_base()}-kirim-{rec.send_attempt + 1}")
            except AksaTaxError as exc:
                result = rec._pending_after_conflict(client, exc)
                if not result:
                    rec.write({"last_error": exc.full_message()})
                    raise UserError(exc.full_message()) from exc
            rec._record_send_result(result, rec.send_attempt + 1)
        return True

    def _record_send_result(self, result, attempt):
        """Jawaban POST /faktur/{id}/kirim. PENDING_APPROVAL = masuk antrean persetujuan internal PajakMu:
        belum terkirim ke DJP (submitted_at kosong); PajakMu mengirimnya otomatis setelah disetujui."""
        self.ensure_one()
        state = REMOTE_STATES.get((result.get("status") or "").upper(), "submitted")
        vals = {"state": state, "send_mode": "send", "send_attempt": attempt, "last_error": False}
        if state == "pending_approval":
            vals["approval_step"] = self._approval_step_label(result.get("persetujuan"))
        else:
            vals["submitted_at"] = fields.Datetime.now()
        self.write(vals)
        if state == "pending_approval":
            self._notify_pending_approval()

    def _pending_after_conflict(self, client, exc):
        """PajakMu lama menjawab 409 (bukan 202 PENDING_APPROVAL) bila faktur masuk persetujuan internal.
        Status sebenarnya dibaca dari GET /faktur/{id}, bukan dari teks galat. Mengembalikan data faktur
        bila memang menunggu persetujuan, selain itu None (galat tetap galat)."""
        self.ensure_one()
        if exc.status != 409 or not self.aksatax_id:
            return None
        try:
            data = client.get_faktur(self.aksatax_id)
        except AksaTaxError:
            return None
        return data if (data.get("status") or "").upper() == "PENDING_APPROVAL" else None

    def _check_no_live_retur(self):
        """e-Faktur yang punya nota retur berlaku tidak boleh dibatalkan/diganti."""
        for rec in self:
            fakturs = rec.move_id.with_context(active_test=False).sudo().coretax_faktur_ids
            live = self.env["coretax.retur"].sudo().search_count([
                ("faktur_id", "in", fakturs.ids), ("state", "in", RETUR_LIVE_STATES)])
            if live:
                raise UserError(_("Faktur ini punya nota retur yang masih berlaku. Batalkan nota retur dulu."))

    def action_open_cancel_wizard(self):
        self.ensure_one()
        self._check_no_live_retur()
        if self.state != "approved" or not self.aksatax_id:
            raise UserError(_("Hanya e-Faktur yang sudah disetujui DJP yang bisa dibatalkan."))
        return {"type": "ir.actions.act_window", "res_model": "coretax.faktur.cancel.wizard",
                "view_mode": "form", "target": "new", "name": _("Batalkan e-Faktur"),
                "context": {"default_faktur_id": self.id}}

    def action_request_replacement(self):
        """Buka penggantian: faktur Odoo boleh di-reset ke draft, diperbaiki, lalu dikirim sebagai faktur pengganti."""
        self._check_no_live_retur()
        for rec in self:
            if rec.replaces_id:
                # PajakMu memakai ulang NSFP faktur asal dengan fgPengganti=1: satu faktur hanya diganti sekali.
                raise UserError(_("e-Faktur ini sudah merupakan pengganti. "
                                  "Penggantian berikutnya lakukan di PajakMu atau DJP."))
            if rec.state != "approved" or rec.replace_requested:
                raise UserError(_("Hanya e-Faktur yang sudah disetujui DJP dan belum dibuka penggantiannya yang bisa diganti."))
            rec.replace_requested = True
            rec.move_id.message_post(body=_(
                "Penggantian e-Faktur dibuka. Reset faktur ke draft, perbaiki, lalu kirim lagi sebagai faktur pengganti."))
        return True

    def action_cancel_replacement(self):
        for rec in self:
            if not rec.replace_requested:
                raise UserError(_("Penggantian e-Faktur ini belum dibuka."))
            if self.with_context(active_test=False).search_count([("replaces_id", "=", rec.id)]):
                raise UserError(_("Penggantian sudah dikirim. Perbaiki lalu kirim ulang faktur pengganti dari faktur Odoo."))
            if rec.move_id.state != "posted":
                # faktur draft tanpa penggantian terbuka tidak lagi cocok dengan e-Faktur yang berlaku di DJP
                raise UserError(_("Posting faktur dulu, lalu batalkan penggantian."))
            rec.replace_requested = False
            rec.move_id.message_post(body=_("Penggantian e-Faktur dibatalkan."))
        return True

    def action_refresh(self):
        for rec in self.filtered("aksatax_id"):
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            try:
                rec._apply_remote(connection._client(rec).get_faktur(rec.aksatax_id))
            except AksaTaxError as exc:
                raise UserError(exc.full_message()) from exc
        return True

    def action_download_pdf(self):
        """Unduh PDF faktur pajak dari PajakMu, simpan sebagai lampiran faktur Odoo
        (satu lampiran per nomor faktur, ditimpa bila diunduh ulang)."""
        self.ensure_one()
        if self.state not in PDF_STATES or not self.aksatax_id:
            raise UserError(_("PDF faktur pajak baru tersedia setelah e-Faktur disetujui DJP. "
                              "Perbarui status e-Faktur lalu coba lagi."))
        connection = self.env["coretax.connection"]._for_company(self.company_id)
        try:
            content = connection._client(self).faktur_pdf(self.aksatax_id)
        except AksaTaxError as exc:
            raise UserError(exc.full_message()) from exc
        if not content:
            raise UserError(_("PDF dari PajakMu kosong. Coba lagi nanti atau periksa dokumennya di PajakMu."))
        if not content.startswith(b"%PDF-"):
            raise UserError(_("Berkas dari PajakMu bukan PDF. Coba lagi nanti atau unduh dari PajakMu."))
        name = f"Faktur Pajak {self.nomor_faktur or self.aksatax_id}.pdf"
        # Pengguna sudah lolos hak akses coretax.faktur; PDF milik faktur ini, jadi lampiran ditulis sebagai sudo
        # (hak baca faktur saja tidak cukup untuk membuat lampiran). Unduhan tetap memeriksa hak baca faktur.
        Attachment = self.env["ir.attachment"].sudo()
        attachment = Attachment.search([
            ("res_model", "=", "account.move"), ("res_id", "=", self.move_id.id), ("name", "=", name)], limit=1)
        if attachment:
            attachment.write({"raw": content, "mimetype": "application/pdf"})
        else:
            attachment = Attachment.create({
                "name": name, "raw": content, "mimetype": "application/pdf",
                "res_model": "account.move", "res_id": self.move_id.id})
        return {"type": "ir.actions.act_url", "url": f"/web/content/{attachment.id}?download=true",
                "target": "self"}

    def action_recreate(self):
        """Buat ulang e-Faktur dari faktur Odoo (setelah ditolak/dibatalkan/gagal)."""
        self.ensure_one()
        if self.state not in RECREATE_STATES:
            raise UserError(_("Hanya e-Faktur yang ditolak, dibatalkan, atau gagal yang bisa dibuat ulang."))
        return self.move_id.action_coretax_send()

    def action_open_aksatax(self):
        self.ensure_one()
        if not self.aksatax_url:
            raise UserError(_("Dokumen ini belum ada di PajakMu."))
        return {"type": "ir.actions.act_url", "url": self.aksatax_url, "target": "new"}

    def action_view_invoice(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "account.move", "res_id": self.move_id.id,
                "views": [[False, "form"]], "target": "current"}

    def action_view_logs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("coretax_integration.action_coretax_log")
        action["domain"] = [("res_model", "=", self._name), ("res_id", "=", self.id)]
        return action

    # ── Ringkasan (dashboard) ─────────────────────────────────────────
    @api.model
    def get_dashboard_data(self):
        company = self.env.company
        connection = self.env["coretax.connection"]._for_company(company, raise_if_missing=False)
        Faktur = self.with_company(company)
        domain = [("company_id", "=", company.id)]
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        state_labels = dict(STATES)
        # masa pajak berjalan, menurut tanggal faktur
        month_domain = domain + [("invoice_date", ">=", month_start),
                                 ("invoice_date", "<", month_start + relativedelta(months=1))]
        [(approved_month, approved_dpp, approved_ppn)] = Faktur._read_group(
            month_domain + [("state", "=", "approved")], [], ["__count", "total_dpp:sum", "total_ppn:sum"])
        state_order = [state for state, _label in STATES]
        by_state = sorted(Faktur._read_group(month_domain, ["state"], ["__count"]),
                          key=lambda row: state_order.index(row[0]))
        month = {
            "start": fields.Date.to_string(month_start), "approved": approved_month,
            "dpp": approved_dpp, "ppn": approved_ppn,
            "by_state": [{"state": state, "label": state_labels[state], "count": count} for state, count in by_state],
        }
        ready = self.env["account.move"].search_count(self.env["account.move"]._coretax_ready_domain())
        counts = {
            "ready": ready,
            "in_progress": Faktur.search_count(domain + [("in_progress", "=", True)]),
            "approved_month": approved_month,
            "attention": Faktur.search_count(domain + [("attention", "=", True)]),
            "rejected": Faktur.search_count(domain + [("state", "=", "rejected")]),
            "failed": Faktur.search_count(domain + [("attention", "=", True), ("state", "!=", "rejected")]),
            "draft": Faktur.search_count(domain + [("state", "=", "draft")]),
            "bupot_open": self.env["coretax.bupot"].search_count(
                [("company_id", "=", company.id), ("state", "in", ["new", "error"])]),
            "partners_unmatched": self.env["res.partner"].search_count(
                self.env["res.partner"]._coretax_unmatched_domain()),
        }
        for kind, (model, kind_domain, _title) in self._readiness_domains().items():
            counts[kind] = self.env[model].search_count(kind_domain)
        recent = [{
            "id": f.id, "name": f.name, "partner": f.partner_id.display_name or "",
            "state": f.state, "state_label": state_labels.get(f.state, f.state),
            "date": fields.Datetime.to_string(f.write_date), "error": f.last_error or "",
        } for f in Faktur.search(domain, order="write_date desc", limit=8)]
        conn = False
        if connection:
            conn = {
                "id": connection.id, "state": connection.state, "environment": connection.environment,
                "state_label": dict(connection._fields["state"].selection).get(connection.state),
                "environment_label": dict(connection._fields["environment"].selection).get(connection.environment),
                "last_sync": fields.Datetime.to_string(connection.last_sync) if connection.last_sync else False,
                "has_api_key": connection.has_api_key, "url": connection.base_url,
            }
        return {"company": company.name, "connection": conn, "counts": counts, "month": month, "recent": recent,
                "currency_id": self.env.ref("base.IDR").id,
                "is_manager": self.env.user.has_group("coretax_integration.group_coretax_manager")}

    # ── Kesiapan data ─────────────────────────────────────────────────
    @api.model
    def _readiness_domains(self):
        """Data master yang membuat e-Faktur kurang tepat: {jenis: (model, domain, judul)}."""
        generic_codes = [self.env.ref(xmlid, raise_if_not_found=False) for xmlid in GENERIC_PRODUCT_CODES]
        uoms = self.env["product.template"]._read_group([("sale_ok", "=", True)], ["uom_id"])
        connection = self.env["coretax.connection"]._for_company(self.env.company, raise_if_missing=False)
        ready_moves = self.env["account.move"].search(self.env["account.move"]._coretax_ready_domain())
        partners = ready_moves.commercial_partner_id.filtered(lambda p: not p._coretax_identity(connection))
        return {
            "products_generic": ("product.template", [
                ("sale_ok", "=", True), "|", ("l10n_id_product_code", "=", False),
                ("l10n_id_product_code", "in", [code.id for code in generic_codes if code]),
            ], _("Produk berkode umum")),
            "uoms_no_code": ("uom.uom", [("id", "in", [uom.id for (uom,) in uoms if uom and not uom.l10n_id_uom_code])],
                             _("Satuan tanpa kode Coretax")),
            "partners_no_id": ("res.partner", [("id", "in", partners.ids)], _("Pelanggan tanpa NPWP/NIK")),
        }

    @api.model
    def action_readiness(self, kind):
        model, domain, title = self._readiness_domains()[kind]
        views = {
            "product.template": [[self.env.ref("coretax_integration.view_coretax_product_code_list").id, "tree"],
                                 [False, "form"]],
            "uom.uom": [[self.env.ref("coretax_integration.view_coretax_uom_code_list").id, "tree"]],
            "res.partner": [[self.env.ref("coretax_integration.view_partner_list_coretax").id, "tree"], [False, "form"]],
        }[model]
        return {"type": "ir.actions.act_window", "name": title, "res_model": model, "domain": domain,
                "views": views, "target": "current", "context": {"create": False}}
