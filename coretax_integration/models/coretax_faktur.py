from datetime import date, datetime, time, timezone

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.client import AksaTaxError

# Status dokumen AksaTax (packages/shared/src/enums.ts) -> status di Odoo.
# "error" khusus Odoo: gagal membuat/mengirim ke AksaTax.
STATES = [
    ("draft", "Draft di AksaTax"),
    ("pending_approval", "Menunggu persetujuan di AksaTax"),
    ("validated", "Siap dikirim ke DJP"),
    ("submitted", "Dikirim ke DJP"),
    ("processing", "Diproses DJP"),
    ("approved", "Disetujui DJP"),
    ("rejected", "Ditolak DJP"),
    ("cancel_requested", "Pembatalan diajukan"),
    ("cancelled", "Dibatalkan"),
    ("error", "Gagal - perlu tindakan"),
]
REMOTE_STATES = {
    "DRAFT": "draft", "PENDING_APPROVAL": "pending_approval", "VALIDATED": "validated",
    "SUBMITTED": "submitted", "PROCESSING": "processing", "APPROVED": "approved",
    "REJECTED": "rejected", "CANCEL_REQUESTED": "cancel_requested", "CANCELLED": "cancelled",
}
# Status yang masih "hidup" - faktur Odoo yang punya e-Faktur seperti ini tidak
# dikirim ulang dan tidak boleh dikembalikan ke draft.
LOCKING_STATES = ("submitted", "processing", "approved", "cancel_requested")
# Bisa dikirim ke DJP dari Odoo (endpoint /kirim memvalidasi draft dulu).
SENDABLE_STATES = ("draft", "validated")
# Boleh dibuat ulang dari Odoo (dokumen lama diarsipkan).
RECREATE_STATES = ("rejected", "cancelled", "error")
ATTENTION_STATES = ("rejected", "error")
IN_PROGRESS_STATES = ("pending_approval", "validated", "submitted", "processing", "cancel_requested")
# Kode barang/jasa umum 000000 yang diberikan Odoo ke produk baru (l10n_id_efaktur_coretax).
GENERIC_PRODUCT_CODES = ("l10n_id_efaktur_coretax.product_code_000000_goods",
                         "l10n_id_efaktur_coretax.product_code_000000_service")


def _parse_dt(value):
    """Tanggal/waktu ISO 8601 dari AksaTax -> datetime UTC tanpa tzinfo
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
    """e-Faktur Keluaran di AksaTax untuk satu faktur pelanggan Odoo.

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
    send_mode = fields.Selection([("send", "Kirim ke DJP"), ("draft", "Draft di AksaTax")],
                                 string="Cara kirim", default="send", readonly=True)

    aksatax_id = fields.Char(string="ID AksaTax", readonly=True, copy=False, index=True)
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
    last_error = fields.Text(string="Keterangan dari AksaTax/DJP", readonly=True)
    error_code = fields.Char(string="Kode galat", readonly=True, copy=False)
    cancel_attempt = fields.Integer(string="Percobaan batal", default=0, readonly=True, copy=False)
    send_attempt = fields.Integer(string="Percobaan kirim", default=0, readonly=True, copy=False)
    submitted_at = fields.Datetime(string="Dikirim ke DJP pada", readonly=True, copy=False)
    last_sync = fields.Datetime(string="Diperbarui dari AksaTax", readonly=True, copy=False)
    aksatax_url = fields.Char(string="Tautan AksaTax", compute="_compute_aksatax_url")
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

    # ── pembaruan dari AksaTax ────────────────────────────────────────
    def _apply_remote(self, data):
        """Isi dokumen dari bentuk `publicFaktur` AksaTax."""
        self.ensure_one()
        vals = {"last_sync": fields.Datetime.now()}
        state = REMOTE_STATES.get((data.get("status") or "").upper())
        if state:
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
        if "lastError" in data:
            vals["last_error"] = data.get("lastError") or False
        if state and state not in ATTENTION_STATES and "lastError" not in data:
            vals["last_error"] = False
        self.write(vals)

    def _apply_event(self, event, connection):
        """Event faktur.* (webhook / polling). Data event ringkas, jadi untuk
        dokumen yang disetujui detailnya diambil lengkap (nomor, kode DJP)."""
        self.ensure_one()
        data = event.get("data") or {}
        vals = {"last_sync": fields.Datetime.now()}
        state = REMOTE_STATES.get((data.get("status") or "").upper()) or {
            "faktur.submitted": "submitted", "faktur.approved": "approved",
            "faktur.rejected": "rejected", "faktur.cancelled": "cancelled",
        }.get(event.get("type"))
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
        """Kirim e-Faktur yang masih draft di AksaTax ke DJP."""
        for rec in self:
            if rec.state not in SENDABLE_STATES or not rec.aksatax_id:
                raise UserError(_("e-Faktur %s tidak dalam status yang bisa dikirim ke DJP.", rec.display_name))
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            try:
                result = connection._client(rec).send_faktur(
                    rec.aksatax_id, f"{rec._idempotency_base()}-kirim-{rec.send_attempt + 1}")
            except AksaTaxError as exc:
                rec.write({"last_error": exc.full_message()})
                raise UserError(exc.full_message()) from exc
            rec.write({"state": REMOTE_STATES.get(result.get("status"), "submitted"), "send_mode": "send",
                       "send_attempt": rec.send_attempt + 1, "submitted_at": fields.Datetime.now(), "last_error": False})
        return True

    def action_open_cancel_wizard(self):
        self.ensure_one()
        if self.state != "approved" or not self.aksatax_id:
            raise UserError(_("Hanya e-Faktur yang sudah disetujui DJP yang bisa dibatalkan."))
        return {"type": "ir.actions.act_window", "res_model": "coretax.faktur.cancel.wizard",
                "view_mode": "form", "target": "new", "name": _("Batalkan e-Faktur"),
                "context": {"default_faktur_id": self.id}}

    def action_refresh(self):
        for rec in self.filtered("aksatax_id"):
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            try:
                rec._apply_remote(connection._client(rec).get_faktur(rec.aksatax_id))
            except AksaTaxError as exc:
                raise UserError(exc.full_message()) from exc
        return True

    def action_recreate(self):
        """Buat ulang e-Faktur dari faktur Odoo (setelah ditolak/dibatalkan/gagal)."""
        self.ensure_one()
        if self.state not in RECREATE_STATES:
            raise UserError(_("Hanya e-Faktur yang ditolak, dibatalkan, atau gagal yang bisa dibuat ulang."))
        return self.move_id.action_coretax_send()

    def action_open_aksatax(self):
        self.ensure_one()
        if not self.aksatax_url:
            raise UserError(_("Dokumen ini belum ada di AksaTax."))
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
            "product.template": [[self.env.ref("coretax_integration.view_coretax_product_code_list").id, "list"],
                                 [False, "form"]],
            "uom.uom": [[self.env.ref("coretax_integration.view_coretax_uom_code_list").id, "list"]],
            "res.partner": [[self.env.ref("coretax_integration.view_partner_list_coretax").id, "list"], [False, "form"]],
        }[model]
        return {"type": "ir.actions.act_window", "name": title, "res_model": model, "domain": domain,
                "views": views, "target": "current", "context": {"create": False}}
