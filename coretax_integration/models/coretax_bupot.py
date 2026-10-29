import math
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..tools.client import AksaTaxError
from .coretax_faktur import REMOTE_STATES

BUPOT_STATES = [
    ("new", "Belum dikirim ke AksaTax"),
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
DOK_DASAR = [
    ("01", "Faktur Pajak"), ("02", "Invoice"), ("03", "Pengumuman"), ("04", "Surat Perjanjian"),
    ("05", "Bukti Pembayaran"), ("06", "Akta Perikatan"), ("07", "Akta RUPS"), ("08", "Surat Pernyataan"),
]
EDITABLE_STATES = ("new", "error")
INACTIVE_STATES = ("rejected", "cancelled")
FASILITAS = [
    ("NONE", "Tanpa fasilitas"), ("SKB", "Surat Keterangan Bebas (SKB)"), ("SKD", "Surat Keterangan Domisili (SKD/P3B)"),
    ("DTP", "Ditanggung Pemerintah (DTP)"), ("PP23", "PP 23 / PP 55 (UMKM)"), ("LAINNYA", "Fasilitas lainnya"),
]


class CoretaxTaxObject(models.Model):
    """Kode objek pajak PPh untuk Bukti Potong Unifikasi (format 99-999-99)."""

    _name = "coretax.tax.object"
    _description = "Kode Objek Pajak PPh"
    _order = "code"
    _rec_names_search = ["code", "name"]

    code = fields.Char(string="Kode objek", required=True)
    name = fields.Char(string="Uraian", required=True)
    jenis = fields.Selection([("BPPU", "Penerima dalam negeri (BPPU)"), ("BPNR", "Penerima luar negeri (BPNR)")],
                             string="Jenis bukti potong", required=True, default="BPPU")
    pasal = fields.Char(string="Pasal PPh", help="Mis. 23, 4(2), 22, 26")
    tarif = fields.Float(string="Tarif (%)", digits=(5, 2))
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint("unique(code)", "Kode objek pajak sudah ada.")

    @api.depends("code", "name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.code} – {rec.name}"

    @api.constrains("code")
    def _check_code(self):
        for rec in self:
            if not re.fullmatch(r"\d{2}-\d{3}-\d{2}", rec.code or ""):
                raise ValidationError(_("Format kode objek pajak harus 99-999-99, mis. 24-104-01."))


class CoretaxBupot(models.Model):
    """Bukti Potong PPh Unifikasi yang dikirim ke AksaTax sebagai DRAFT.
    Penandatanganan dan pengiriman ke DJP dilakukan di AksaTax (Public API
    AksaTax belum menyediakan pengiriman bukti potong)."""

    _name = "coretax.bupot"
    _description = "Bukti Potong PPh (Coretax)"
    _inherit = ["mail.thread"]
    _order = "tanggal_pemotongan desc, id desc"
    _check_company_auto = True

    _bill_unique = models.UniqueIndex(
        "(move_id, tax_object_id) WHERE origin = 'bill' AND state NOT IN ('rejected', 'cancelled')",
        "Tagihan ini sudah punya bukti potong aktif untuk kode objek pajak yang sama.")

    name = fields.Char(string="Bukti Potong", compute="_compute_name", store=True)
    company_id = fields.Many2one("res.company", string="Perusahaan", required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.ref("base.IDR"), readonly=True)
    move_id = fields.Many2one("account.move", string="Tagihan vendor", check_company=True,
                              domain="[('move_type', 'in', ('in_invoice', 'in_receipt'))]")
    partner_id = fields.Many2one("res.partner", string="Penerima penghasilan", required=True, tracking=True)
    partner_match_state = fields.Selection(related="partner_id.coretax_match_state", string="Status di AksaTax")

    jenis = fields.Selection([("BPPU", "Penerima dalam negeri (BPPU)"), ("BPNR", "Penerima luar negeri (BPNR)")],
                             string="Jenis", required=True, default="BPPU", tracking=True)
    tax_object_id = fields.Many2one("coretax.tax.object", string="Kode objek pajak", required=True, tracking=True,
                                    domain="[('jenis', '=', jenis)]")
    origin = fields.Selection([("bill", "Dari tagihan"), ("manual", "Manual")], string="Asal", default="manual",
                              readonly=True, required=True)
    tarif = fields.Float(string="Tarif (%)", compute="_compute_tarif", store=True, readonly=False, tracking=True)
    tanggal_pemotongan = fields.Date(string="Tanggal pemotongan", required=True, default=fields.Date.context_today,
                                     tracking=True)
    jumlah_bruto = fields.Monetary(string="Jumlah bruto (DPP)", required=True, tracking=True,
                                   help="Nilai penghasilan sebelum dipotong pajak, dalam Rupiah.")
    kira_netto = fields.Float(string="Perkiraan penghasilan neto (%)", default=100.0,
                              help="Untuk PPh 26 (BPNR). Biarkan 100 bila tidak ada ketentuan khusus.")
    pph_estimasi = fields.Monetary(string="Perkiraan PPh dipotong", compute="_compute_pph_estimasi")
    fasilitas = fields.Selection(FASILITAS, string="Fasilitas pajak", default="NONE", required=True, tracking=True)
    nomor_fasilitas = fields.Char(string="Nomor dokumen fasilitas", tracking=True)
    tarif_fasilitas = fields.Float(string="Tarif P3B (%)", help="Wajib untuk fasilitas SKD.")
    dok_jenis = fields.Selection(DOK_DASAR, string="Jenis dokumen dasar", default="02", required=True,
                            tracking=True)
    dok_nomor = fields.Char(string="Nomor dokumen dasar", required=True, tracking=True)
    dok_tanggal = fields.Date(string="Tanggal dokumen dasar", required=True, default=fields.Date.context_today,
                               tracking=True)
    ajukan = fields.Boolean(string="Langsung ajukan persetujuan di AksaTax")

    state = fields.Selection(BUPOT_STATES, string="Status", default="new", required=True, tracking=True, index=True)
    aksatax_id = fields.Char(string="ID AksaTax", readonly=True, copy=False, index=True)
    nomor_bupot = fields.Char(string="Nomor bukti potong", readonly=True, copy=False, tracking=True)
    last_error = fields.Text(string="Keterangan dari AksaTax/DJP", readonly=True, copy=False)
    version = fields.Integer(default=1, readonly=True, copy=False)
    last_sync = fields.Datetime(string="Diperbarui dari AksaTax", readonly=True, copy=False)
    aksatax_url = fields.Char(compute="_compute_aksatax_url")

    source_line_ids = fields.Many2many("account.move.line", string="Baris sumber", compute="_compute_source_line_ids")
    tax_line_ids = fields.Many2many("account.move.line", string="Journal item PPh", compute="_compute_tax_line_ids")
    pph_gl = fields.Monetary(string="PPh di jurnal", compute="_compute_pph_gl")
    pph_selisih = fields.Monetary(string="Selisih PPh jurnal vs bukti potong", compute="_compute_pph_selisih")

    @api.depends("tax_object_id")
    def _compute_tarif(self):
        for rec in self:
            rec.tarif = rec.tax_object_id.tarif

    def _compute_source_line_ids(self):
        for rec in self:
            if rec.origin == "bill" and rec.move_id:
                rec.source_line_ids = rec.move_id._coretax_pph_groups().get(rec.tax_object_id)
            else:
                rec.source_line_ids = False

    def _compute_tax_line_ids(self):
        for rec in self:
            if rec.origin == "bill" and rec.move_id:
                rec.tax_line_ids = rec.move_id._coretax_pph_family().line_ids.filtered(
                    lambda l: l.tax_line_id.coretax_tax_object_id and l.coretax_tax_object_id == rec.tax_object_id)
            else:
                rec.tax_line_ids = False

    def _compute_pph_gl(self):
        for rec in self:
            rec.pph_gl = -sum(rec.tax_line_ids.mapped("balance"))

    @api.depends("pph_estimasi")
    def _compute_pph_selisih(self):
        for rec in self:
            rec.pph_selisih = -sum(rec.tax_line_ids.mapped("balance")) - rec.pph_estimasi

    def unlink(self):
        if not self.env.context.get("coretax_bupot_sync") and self.filtered(lambda b: b.origin == "bill" and b.move_id):
            raise UserError(_("Bukti potong dari tagihan vendor mengikuti tagihannya. "
                              "Ubah, batalkan, atau hapus tagihannya."))
        return super().unlink()

    def _coretax_is_at_aksatax(self):
        self.ensure_one()
        return bool(self.aksatax_id) and self.state not in INACTIVE_STATES

    @api.depends("nomor_bupot", "partner_id", "tax_object_id")
    def _compute_name(self):
        for rec in self:
            rec.name = rec.nomor_bupot or _("Draft – %s", rec.partner_id.display_name or _("tanpa penerima"))

    @api.depends("jumlah_bruto", "tarif", "kira_netto", "fasilitas")
    def _compute_pph_estimasi(self):
        for rec in self:
            if rec.fasilitas in ("SKB", "DTP"):
                rec.pph_estimasi = 0
                continue
            neto = rec.kira_netto if rec.jenis == "BPNR" else 100.0
            rate = rec.tarif_fasilitas if rec.fasilitas == "SKD" and rec.tarif_fasilitas else rec.tarif
            rec.pph_estimasi = math.floor((rec.jumlah_bruto or 0) * neto / 100 * (rate or 0) / 100)

    def _compute_aksatax_url(self):
        for rec in self:
            connection = self.env["coretax.connection"]._for_company(rec.company_id, raise_if_missing=False)
            base = (connection.base_url or "").rstrip("/")
            rec.aksatax_url = f"{base}/bupot-unifikasi" if base else False

    @api.onchange("jenis")
    def _onchange_jenis(self):
        if self.tax_object_id and self.tax_object_id.jenis != self.jenis:
            self.tax_object_id = False

    def _payload(self, counterparty_id):
        self.ensure_one()
        payload = {
            "jenis": self.jenis, "counterpartyId": counterparty_id, "kodeObjek": self.tax_object_id.code,
            "tanggalPemotongan": fields.Date.to_string(self.tanggal_pemotongan),
            "jumlahBruto": int(round(self.jumlah_bruto)), "kiraNetto": self.kira_netto if self.jenis == "BPNR" else 100,
            "fasilitas": self.fasilitas,
            "dokumenDasar": [{"jenisDok": self.dok_jenis, "nomorDok": self.dok_nomor[:100],
                              "tanggalDok": fields.Date.to_string(self.dok_tanggal)}],
            "ajukan": bool(self.ajukan),
        }
        if self.fasilitas != "NONE":
            payload["nomorFasilitas"] = self.nomor_fasilitas or ""
        if self.fasilitas == "SKD":
            payload["tarifFasilitas"] = self.tarif_fasilitas
        return payload

    def _check_before_send(self):
        self.ensure_one()
        problems = []
        if self.origin == "bill" and self.move_id.state != "posted":
            problems.append(_("Tagihan vendor %s belum diposting.", self.move_id.display_name))
        if self.jumlah_bruto <= 0:
            problems.append(_("Jumlah bruto harus lebih dari 0."))
        if self.fasilitas != "NONE" and not self.nomor_fasilitas:
            problems.append(_("Isi nomor dokumen fasilitas (%s).", dict(FASILITAS)[self.fasilitas]))
        if self.fasilitas == "SKD" and not self.tarif_fasilitas:
            problems.append(_("Isi tarif P3B untuk fasilitas SKD."))
        if self.jenis == "BPNR" and not self.tax_object_id.code.startswith("27-"):
            problems.append(_("Penerima luar negeri (BPNR) memakai kode objek PPh 26 (27-xxx-xx)."))
        if self.jenis == "BPPU" and self.tax_object_id.code.startswith("27-"):
            problems.append(_("Kode objek PPh 26 hanya untuk penerima luar negeri (BPNR)."))
        if not self.partner_id._coretax_identity():
            problems.append(_("Penerima %s belum punya NPWP/NIK.", self.partner_id.display_name))
        if problems:
            raise UserError("\n".join(problems))

    def action_send_draft(self):
        for rec in self:
            if rec.state not in ("new", "error"):
                raise UserError(_("Bukti potong %s sudah ada di AksaTax.", rec.display_name))
            rec._check_before_send()
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            client = connection._client(rec)
            partner = rec.partner_id.commercial_partner_id.with_company(rec.company_id)
            try:
                counterparty = partner._coretax_counterparty_for(connection)
                if not counterparty:
                    raise AksaTaxError(_("Penerima %s belum terdaftar di AksaTax. Klik 'Daftarkan ke AksaTax' di "
                                         "Coretax › Data Master › Lawan Transaksi, lalu kirim ulang.",
                                         partner.display_name), code="COUNTERPARTY_MISSING")
                uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid", "odoo")
                data = client.create_bupot_unifikasi(rec._payload(counterparty), f"odoo-{uuid}-bupot-{rec.id}-v{rec.version}")
            except AksaTaxError as exc:
                rec.write({"state": "error", "last_error": exc.full_message(), "version": rec.version + 1})
                continue
            rec.write({
                "aksatax_id": data.get("id"), "state": REMOTE_STATES.get((data.get("status") or "").upper(), "draft"),
                "nomor_bupot": data.get("nomorBupot") or False, "last_error": data.get("lastError") or False,
                "last_sync": fields.Datetime.now(),
            })
            rec.message_post(body=_("Draft bukti potong dibuat di AksaTax. Tanda tangani & kirim ke DJP dari AksaTax."))
        return True

    def action_refresh(self):
        for rec in self.filtered("aksatax_id"):
            connection = self.env["coretax.connection"]._for_company(rec.company_id)
            try:
                rec._apply_status(connection._client(rec).document_status("bupot-unifikasi", rec.aksatax_id))
            except AksaTaxError as exc:
                raise UserError(exc.full_message()) from exc
        return True

    def _apply_status(self, data):
        """Bentuk /public/dokumen/{jenis}/{id}: {status, nomor, lastError}."""
        self.ensure_one()
        vals = {"last_sync": fields.Datetime.now()}
        state = REMOTE_STATES.get((data.get("status") or "").upper())
        if state:
            vals["state"] = state
        if data.get("nomor"):
            vals["nomor_bupot"] = data["nomor"]
        if "lastError" in data:
            vals["last_error"] = data.get("lastError") or False
        self.write(vals)

    def _apply_event(self, event, connection):
        data = event.get("data") or {}
        state = {"bupot.approved": "APPROVED", "bupot.rejected": "REJECTED", "bupot.cancelled": "CANCELLED"}.get(
            event.get("type"))
        self._apply_status({"status": data.get("status") or state, "nomor": data.get("nomor"),
                            **({"lastError": data["alasan"]} if data.get("alasan") else {})})

    def action_open_aksatax(self):
        self.ensure_one()
        if not self.aksatax_url:
            raise UserError(_("Koneksi AksaTax belum diatur."))
        return {"type": "ir.actions.act_url", "url": self.aksatax_url, "target": "new"}
