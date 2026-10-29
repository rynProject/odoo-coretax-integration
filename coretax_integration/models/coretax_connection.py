import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

from ..tools.client import AksaTaxClient, AksaTaxError

_logger = logging.getLogger(__name__)

# Event AksaTax yang diikuti Odoo (lewat webhook maupun /public/events).
EVENT_TYPES = [
    "faktur.submitted", "faktur.approved", "faktur.rejected", "faktur.cancelled",
    "bupot.approved", "bupot.rejected", "bupot.cancelled",
    "retur.approved", "retur.rejected", "retur.cancelled",
]
# Halaman event yang diambil per putaran sinkronisasi (200 event per halaman).
EVENT_PAGES_PER_RUN = 10
# Dokumen yang masih diproses dicek ulang statusnya setelah selang ini.
REFRESH_AFTER_MINUTES = 10
REFRESH_BATCH = 40

# Pemetaan data lawan transaksi -> field kontak Odoo bawaan.
DEFAULT_PARTNER_MAPPING = {
    "npwp": "vat",
    "nik": "l10n_id_nik",
    "doc_number": "l10n_id_buyer_document_number",
    "tku": "l10n_id_tku",
    "name": "name",
    "address": "contact_address",
    "email": "email",
}
PARTNER_FIELD_DOMAIN = "[('model', '=', 'res.partner'), ('ttype', 'in', ('char', 'text', 'selection', 'many2one'))]"


class CoretaxConnection(models.Model):
    """Koneksi ke AksaTax untuk satu perusahaan. Satu API key AksaTax mewakili
    satu wajib pajak (NPWP), jadi setiap perusahaan di Odoo punya koneksinya
    sendiri."""

    _name = "coretax.connection"
    _description = "Koneksi AksaTax"
    _inherit = ["mail.thread"]
    _check_company_auto = True

    name = fields.Char(string="Nama", compute="_compute_name", store=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", string="Perusahaan", required=True, default=lambda self: self.env.company)
    company_vat = fields.Char(related="company_id.vat", string="NPWP Perusahaan")

    base_url = fields.Char(string="Alamat AksaTax", required=True, default="https://app.aksatax.id", tracking=True,
                           help="Alamat aplikasi AksaTax yang Anda pakai, mis. https://app.aksatax.id")
    api_key = fields.Char(string="API Key", copy=False, groups="coretax_integration.group_coretax_manager",
                          help="Dibuat di AksaTax › Integrasi › API Key. Diawali ak_live_ (produksi) atau ak_test_ (sandbox).")
    has_api_key = fields.Boolean(string="API key terisi", compute="_compute_has_api_key")
    environment = fields.Selection([("production", "Produksi"), ("sandbox", "Sandbox (uji coba)"), ("unknown", "Belum diketahui")],
                                   string="Lingkungan", compute="_compute_environment", compute_sudo=True)
    state = fields.Selection([("draft", "Belum diuji"), ("ok", "Terhubung"), ("error", "Gagal terhubung")],
                             string="Status koneksi", default="draft", readonly=True, tracking=True)
    last_check = fields.Datetime(string="Terakhir diuji", readonly=True)
    last_error = fields.Text(string="Pesan galat terakhir", readonly=True)

    auto_mode = fields.Selection([
        ("manual", "Manual - saya kirim sendiri dari faktur"),
        ("draft", "Otomatis simpan sebagai draft di AksaTax saat faktur diposting"),
        ("send", "Otomatis kirim ke DJP saat faktur diposting"),
    ], string="Saat faktur pelanggan diposting", default="manual", required=True, tracking=True)
    default_send_mode = fields.Selection([
        ("send", "Langsung kirim ke DJP"),
        ("draft", "Simpan sebagai draft di AksaTax (disetujui di AksaTax)"),
    ], string="Pilihan awal saat mengirim", default="send", required=True)

    sync_method = fields.Selection([
        ("poll", "Otomatis setiap 5 menit (disarankan)"),
        ("webhook", "Webhook - lebih cepat, butuh alamat Odoo yang bisa diakses dari internet"),
    ], string="Cara menerima hasil DJP", default="poll", required=True)
    webhook_secret = fields.Char(string="Webhook Secret", copy=False, groups="coretax_integration.group_coretax_manager",
                                 help="Diawali whsec_. Ditampilkan sekali saat membuat webhook di AksaTax › Integrasi › Webhook.")
    webhook_url = fields.Char(string="Alamat webhook untuk AksaTax", compute="_compute_webhook_url")
    event_cursor = fields.Integer(string="Posisi event terakhir", default=0, readonly=True, copy=False)
    last_sync = fields.Datetime(string="Sinkronisasi terakhir", readonly=True, copy=False)

    # ── lawan transaksi ───────────────────────────────────────────────
    auto_register_partner = fields.Boolean(
        string="Daftarkan lawan transaksi baru otomatis", default=True,
        help="Saat mengirim e-Faktur atau bukti potong, kontak yang belum ada di AksaTax didaftarkan dulu "
             "sebagai Lawan Transaksi (bila datanya lengkap), lalu dokumennya langsung dikirim.")
    # Field kontak Odoo yang dipakai untuk data lawan transaksi. Bawaan: field standar
    # Odoo/l10n_id; bisa diganti field lain, termasuk field kustom (x_…/Studio).
    map_npwp_field_id = fields.Many2one("ir.model.fields", string="Field NPWP", domain=PARTNER_FIELD_DOMAIN,
                                        default=lambda self: self._default_map("npwp"), ondelete="set null")
    map_nik_field_id = fields.Many2one("ir.model.fields", string="Field NIK", domain=PARTNER_FIELD_DOMAIN,
                                       default=lambda self: self._default_map("nik"), ondelete="set null")
    map_doc_number_field_id = fields.Many2one(
        "ir.model.fields", string="Field nomor paspor / identitas lain", domain=PARTNER_FIELD_DOMAIN,
        default=lambda self: self._default_map("doc_number"), ondelete="set null")
    map_tku_field_id = fields.Many2one("ir.model.fields", string="Field NITKU / TKU", domain=PARTNER_FIELD_DOMAIN,
                                       default=lambda self: self._default_map("tku"), ondelete="set null",
                                       help="22 digit NITKU, atau 6 digit nomor cabang (digabung dengan NPWP).")
    map_name_field_id = fields.Many2one("ir.model.fields", string="Field nama", domain=PARTNER_FIELD_DOMAIN,
                                        default=lambda self: self._default_map("name"), ondelete="set null")
    map_address_field_id = fields.Many2one("ir.model.fields", string="Field alamat", domain=PARTNER_FIELD_DOMAIN,
                                           default=lambda self: self._default_map("address"), ondelete="set null")
    map_email_field_id = fields.Many2one("ir.model.fields", string="Field email", domain=PARTNER_FIELD_DOMAIN,
                                         default=lambda self: self._default_map("email"), ondelete="set null")

    log_count = fields.Integer(compute="_compute_log_count")

    _company_uniq = models.Constraint("unique(company_id)", "Setiap perusahaan hanya boleh punya satu koneksi AksaTax.")

    @api.depends("company_id")
    def _compute_name(self):
        for rec in self:
            rec.name = _("AksaTax – %s", rec.company_id.name or "")

    def _compute_has_api_key(self):
        for rec in self:
            rec.has_api_key = bool(rec.sudo().api_key)

    def _compute_environment(self):
        for rec in self:
            key = rec.sudo().api_key or ""
            rec.environment = "production" if key.startswith("ak_live_") else "sandbox" if key.startswith("ak_test_") else "unknown"

    @api.depends("company_id")
    def _compute_webhook_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for rec in self:
            rec.webhook_url = f"{base.rstrip('/')}/coretax/webhook/{rec.id}" if rec.id else ""

    def _compute_log_count(self):
        counts = dict(self.env["coretax.log"]._read_group([("connection_id", "in", self.ids)], ["connection_id"], ["__count"]))
        for rec in self:
            rec.log_count = counts.get(rec, 0)

    @api.onchange("api_key")
    def _onchange_api_key(self):
        key = (self.api_key or "").strip()
        if key and not key.startswith(("ak_live_", "ak_test_")):
            return {"warning": {"title": _("Format API key"),
                                "message": _("API key AksaTax biasanya diawali ak_live_ atau ak_test_. Periksa lagi salinannya.")}}

    def write(self, vals):
        # kunci/alamat berubah: status koneksi perlu diuji ulang
        if {"api_key", "base_url"} & set(vals):
            vals = dict(vals, state="draft")
        return super().write(vals)

    # ── pemetaan field lawan transaksi ────────────────────────────────
    @api.model
    def _default_map(self, key):
        return self.env["ir.model.fields"]._get("res.partner", DEFAULT_PARTNER_MAPPING[key]).id

    def _partner_field_name(self, key):
        """Nama field kontak untuk satu data lawan transaksi; field bawaan bila belum/tidak lagi dipetakan."""
        field = self[f"map_{key}_field_id"] if self else False
        return field.name if field else DEFAULT_PARTNER_MAPPING[key]

    def action_reset_partner_mapping(self):
        self.ensure_one()
        self.write({f"map_{key}_field_id": self._default_map(key) for key in DEFAULT_PARTNER_MAPPING})
        return True

    # ── akses ─────────────────────────────────────────────────────────
    @api.model
    def _for_company(self, company=None, raise_if_missing=True):
        company = company or self.env.company
        connection = self.sudo().search([("company_id", "=", company.id)], limit=1)
        if not connection and raise_if_missing:
            raise UserError(_("Koneksi AksaTax untuk %s belum diatur. Buka Coretax › Pengaturan › Koneksi AksaTax.", company.name))
        return connection

    @api.model
    def action_open_for_company(self):
        """Menu Pengaturan › Koneksi AksaTax: langsung membuka koneksi milik
        perusahaan aktif (dibuat bila belum ada)."""
        connection = self._for_company(raise_if_missing=False)
        if not connection:
            connection = self.create({"company_id": self.env.company.id})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": connection.id,
                "views": [[False, "form"]], "target": "current", "name": _("Koneksi AksaTax")}

    def _client(self, res=None):
        """Klien API untuk koneksi ini; setiap panggilan dicatat di Riwayat
        Sinkronisasi (cursor terpisah, supaya catatan galat tetap tersimpan
        walau transaksinya dibatalkan)."""
        self.ensure_one()
        connection_id, company_id = self.id, self.company_id.id
        res_model = res._name if res else False
        res_id = res.id if res else False
        document_name = res.display_name if res else False

        def logger(entry):
            vals = dict(entry, connection_id=connection_id, company_id=company_id,
                        res_model=res_model, res_id=res_id, document_name=document_name)
            if odoo_module.current_test:
                # saat pengujian: cursor yang sama (data uji belum di-commit)
                self.env["coretax.log"].sudo().create(vals)
                return
            with self.env.registry.cursor() as cr:
                self.env(cr=cr, su=True)["coretax.log"].create(vals)

        return AksaTaxClient(self.sudo().base_url, self.sudo().api_key, logger=logger)

    # ── tombol ────────────────────────────────────────────────────────
    def _notify(self, title, message, kind="success", sticky=False):
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": sticky,
                       "next": {"type": "ir.actions.client", "tag": "soft_reload"}},
        }

    def action_test_connection(self):
        self.ensure_one()
        try:
            self._client().list_faktur(limit=1)
        except AksaTaxError as exc:
            self.write({"state": "error", "last_check": fields.Datetime.now(), "last_error": exc.full_message()})
            return self._notify(_("Koneksi gagal"), exc.full_message(), "danger", sticky=True)
        self.write({"state": "ok", "last_check": fields.Datetime.now(), "last_error": False})
        env_label = dict(self._fields["environment"].selection).get(self.environment)
        return self._notify(_("Terhubung ke AksaTax"), _("Koneksi berhasil (%s). Odoo siap mengirim e-Faktur.", env_label))

    def action_sync_now(self):
        self.ensure_one()
        try:
            summary = self._sync()
        except AksaTaxError as exc:
            return self._notify(_("Sinkronisasi gagal"), exc.full_message(), "danger", sticky=True)
        return self._notify(_("Sinkronisasi selesai"),
                            _("%(events)s kabar baru dari AksaTax, %(refreshed)s dokumen diperbarui.", **summary))

    def action_open_aksatax(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.base_url, "target": "new"}

    def action_open_aksatax_integrasi(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": f"{self.base_url.rstrip('/')}/integrasi/api", "target": "new"}

    def action_match_all_partners(self):
        self.ensure_one()
        return self.env["res.partner"].with_company(self.company_id).action_coretax_match_all()

    def action_view_logs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("coretax_integration.action_coretax_log")
        action["domain"] = [("connection_id", "=", self.id)]
        return action

    # ── sinkronisasi ──────────────────────────────────────────────────
    @api.model
    def _cron_sync(self):
        for connection in self.sudo().search([]):
            if not connection.api_key:
                continue
            try:
                connection._sync()
                self.env.cr.commit()
            except AksaTaxError as exc:
                self.env.cr.rollback()
                _logger.warning("Sinkronisasi AksaTax %s gagal: %s", connection.company_id.name, exc.full_message())
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Sinkronisasi AksaTax %s gagal", connection.company_id.name)

    def _sync(self):
        """Ambil kabar terbaru dari AksaTax (event), lalu cek ulang dokumen
        yang masih diproses DJP. Aman dijalankan berulang."""
        self.ensure_one()
        self = self.with_company(self.company_id)
        client = self._client()
        processed = 0
        cursor = self.event_cursor
        for _page in range(EVENT_PAGES_PER_RUN):
            result = client.events(after=cursor, types=EVENT_TYPES, limit=200)
            for event in result.get("data") or []:
                self._process_event(event)
                processed += 1
            cursor = result.get("next_after") or cursor
            if not result.get("has_more"):
                break
        refreshed = self._refresh_pending(client)
        self.sudo().write({"event_cursor": cursor, "last_sync": fields.Datetime.now(), "state": "ok", "last_error": False})
        return {"events": processed, "refreshed": refreshed}

    def _refresh_pending(self, client):
        threshold = fields.Datetime.now() - timedelta(minutes=REFRESH_AFTER_MINUTES)
        count = 0
        fakturs = self.env["coretax.faktur"].sudo().search([
            ("company_id", "=", self.company_id.id), ("aksatax_id", "!=", False),
            ("state", "in", ["submitted", "processing", "cancel_requested"]),
            "|", ("last_sync", "=", False), ("last_sync", "<", threshold),
        ], limit=REFRESH_BATCH, order="last_sync asc")
        for faktur in fakturs:
            try:
                faktur._apply_remote(client.get_faktur(faktur.aksatax_id))
                count += 1
            except AksaTaxError as exc:
                if exc.code == "NOT_FOUND":
                    faktur.write({"state": "error", "last_error": exc.user_message, "last_sync": fields.Datetime.now()})
        bupots = self.env["coretax.bupot"].sudo().search([
            ("company_id", "=", self.company_id.id), ("aksatax_id", "!=", False),
            ("state", "in", ["pending_approval", "validated", "submitted", "processing", "cancel_requested"]),
            "|", ("last_sync", "=", False), ("last_sync", "<", threshold),
        ], limit=REFRESH_BATCH, order="last_sync asc")
        for bupot in bupots:
            try:
                bupot._apply_status(client.document_status("bupot-unifikasi", bupot.aksatax_id))
                count += 1
            except AksaTaxError:
                continue
        returs = self.env["coretax.retur"].sudo().search([
            ("company_id", "=", self.company_id.id), ("aksatax_id", "!=", False),
            ("state", "in", ["draft", "submitted", "cancel_requested"]),
            "|", ("last_sync", "=", False), ("last_sync", "<", threshold),
        ], limit=REFRESH_BATCH, order="last_sync asc")
        for retur in returs:
            try:
                retur._apply_remote(client.get_retur(retur.aksatax_id))
                count += 1
            except AksaTaxError as exc:
                if exc.code == "NOT_FOUND":
                    retur._mark_not_found()
                continue
        return count

    def _process_event(self, event):
        """Satu event AksaTax (dari /public/events atau webhook)."""
        kind = (event.get("type") or "").split(".")[0]
        object_id = (event.get("object") or {}).get("id") or (event.get("data") or {}).get("id")
        if not object_id:
            return False
        if kind == "faktur":
            record = self.env["coretax.faktur"].sudo().with_context(active_test=False).search(
                [("aksatax_id", "=", object_id), ("company_id", "=", self.company_id.id)], limit=1)
        elif kind == "bupot":
            record = self.env["coretax.bupot"].sudo().search(
                [("aksatax_id", "=", object_id), ("company_id", "=", self.company_id.id)], limit=1)
        elif kind == "retur":
            record = self.env["coretax.retur"].sudo().search(
                [("aksatax_id", "=", object_id), ("company_id", "=", self.company_id.id)], limit=1)
        else:
            return False
        if not record:
            # dokumen dibuat langsung di AksaTax, bukan dari Odoo - abaikan
            return False
        record._apply_event(event, self)
        return True
