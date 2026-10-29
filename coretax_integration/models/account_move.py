import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..tools.client import AksaTaxError
from .coretax_faktur import LOCKING_STATES, RECREATE_STATES, REMOTE_STATES, STATES
from .coretax_retur import LIVE_STATES as RETUR_LIVE_STATES

_logger = logging.getLogger(__name__)

# Kode transaksi yang bisa dibuat lewat Public API AksaTax (01-10). 07/08 ikut
# mengirim keterangan tambahan; 10 hanya diterima AksaTax versi terbaru.
SUPPORTED_TRX_CODES = {"01", "02", "03", "04", "05", "06", "07", "08", "09", "10"}


class AccountMove(models.Model):
    _inherit = "account.move"

    coretax_vendor_tax_number = fields.Char(string="Nomor Faktur Pajak vendor", copy=False,
                                            help="Nomor faktur pajak dari vendor; dipakai untuk nota retur pembelian.")
    coretax_retur_ids = fields.One2many("coretax.retur", "move_id", string="Nota retur Coretax")
    coretax_retur_count = fields.Integer(compute="_compute_coretax_retur", string="Jumlah nota retur")
    coretax_retur_live = fields.Boolean(compute="_compute_coretax_retur", string="Nota retur masih berlaku")
    coretax_faktur_ids = fields.One2many("coretax.faktur", "move_id", string="e-Faktur Coretax")
    coretax_faktur_id = fields.Many2one("coretax.faktur", string="e-Faktur terakhir",
                                        compute="_compute_coretax_faktur_id", store=True, index=True)
    coretax_state = fields.Selection(string="Status e-Faktur", related="coretax_faktur_id.state", store=True)
    coretax_nomor_faktur = fields.Char(related="coretax_faktur_id.nomor_faktur", string="Nomor Faktur Pajak")
    coretax_last_error = fields.Text(related="coretax_faktur_id.last_error", string="Keterangan e-Faktur")
    coretax_can_send = fields.Boolean(string="Bisa dikirim ke Coretax", compute="_compute_coretax_can_send")
    coretax_faktur_count = fields.Integer(compute="_compute_coretax_faktur_id", store=True)
    coretax_is_replacement = fields.Boolean(string="e-Faktur pengganti", compute="_compute_coretax_is_replacement",
                                            compute_sudo=True)

    @api.constrains("coretax_vendor_tax_number")
    def _check_coretax_vendor_tax_number(self):
        for move in self.filtered("coretax_vendor_tax_number"):
            digits = re.sub(r"\D", "", move.coretax_vendor_tax_number)
            if len(digits) not in (13, 16, 17):
                raise ValidationError(_("Nomor Faktur Pajak vendor harus 13, 16, atau 17 digit."))

    @api.depends("coretax_faktur_id.replaces_id")
    def _compute_coretax_is_replacement(self):
        # e-Faktur pengganti tidak bisa diganti lagi dari Odoo (tombol Ganti e-Faktur disembunyikan)
        for move in self:
            move.coretax_is_replacement = bool(move.coretax_faktur_id.replaces_id)

    @api.depends("coretax_faktur_ids", "coretax_faktur_ids.active", "coretax_faktur_ids.version")
    def _compute_coretax_faktur_id(self):
        for move in self:
            fakturs = move.coretax_faktur_ids.filtered("active").sorted("version", reverse=True)
            move.coretax_faktur_id = fakturs[:1]
            move.coretax_faktur_count = len(move.with_context(active_test=False).coretax_faktur_ids)

    @api.depends("state", "move_type", "coretax_state", "coretax_faktur_id.replace_requested", "line_ids.tax_ids")
    def _compute_coretax_can_send(self):
        for move in self:
            move.coretax_can_send = (
                move._coretax_is_candidate()
                and (not move.coretax_faktur_id or move.coretax_state in RECREATE_STATES
                     or move.coretax_faktur_id == move._coretax_replacing())
            )

    def _coretax_replacing(self):
        """e-Faktur (termasuk yang diarsipkan) yang disetujui dan penggantiannya dibuka, maksimal satu.
        Dicari di semua versi: bila pengganti ditolak, versi terakhir adalah yang ditolak."""
        self.ensure_one()
        return self.sudo().with_context(active_test=False).coretax_faktur_ids.filtered(
            lambda f: f.state == "approved" and f.replace_requested)[:1]

    def _coretax_is_candidate(self):
        self.ensure_one()
        return (self.move_type == "out_invoice" and self.state == "posted" and self.country_code == "ID"
                and bool(self.invoice_line_ids.tax_ids))

    @api.model
    def _coretax_ready_domain(self):
        """Faktur pelanggan terposting ber-PPN yang belum punya e-Faktur aktif."""
        return [
            ("company_id", "=", self.env.company.id), ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
            ("invoice_line_ids.tax_ids", "!=", False),
            "|", "|", ("coretax_faktur_id", "=", False), ("coretax_state", "in", list(RECREATE_STATES)),
            "&", ("coretax_state", "=", "approved"), ("coretax_faktur_id.replace_requested", "=", True),
        ]

    # ── pemeriksaan sebelum kirim ─────────────────────────────────────
    def _coretax_check(self, connection=None):
        """Daftar (tingkat, pesan) untuk satu faktur. 'error' = tidak bisa
        dikirim; 'warning' = boleh dikirim tapi sebaiknya diperbaiki."""
        self.ensure_one()
        issues = []
        if self.move_type != "out_invoice":
            return [("error", _("Hanya faktur pelanggan yang bisa dibuatkan e-Faktur."))]
        if self.state != "posted":
            issues.append(("error", _("Faktur belum diposting (masih draft atau dibatalkan).")))
        if (self.coretax_faktur_id and self.coretax_state not in RECREATE_STATES
                and self.coretax_faktur_id != self._coretax_replacing()):
            label = dict(STATES).get(self.coretax_state)
            issues.append(("error", _("Sudah punya e-Faktur (%s). Tidak dikirim lagi.", label)))
        replacing = self._coretax_replacing()
        if replacing and not replacing.nomor_faktur:
            issues.append(("error", _("Nomor Faktur Pajak e-Faktur lama belum ada. "
                                      "Klik Perbarui status di e-Faktur lama dulu.")))
        if self.currency_id != self.env.ref("base.IDR"):
            issues.append(("error", _("Mata uang faktur %s - e-Faktur harus dalam Rupiah (IDR).", self.currency_id.name)))
        if not self.invoice_line_ids.tax_ids:
            issues.append(("error", _("Tidak ada baris faktur yang dikenai PPN.")))
        if not self.invoice_date:
            issues.append(("error", _("Tanggal faktur belum diisi.")))
        elif self.invoice_date > fields.Date.context_today(self):
            issues.append(("error", _("Tanggal faktur di masa depan - DJP menolak faktur bertanggal ke depan.")))
        code = self.l10n_id_kode_transaksi
        if not code:
            issues.append(("error", _("Kode transaksi e-Faktur belum dipilih (tab Lainnya / Other Info faktur).")))
        elif code not in SUPPORTED_TRX_CODES:
            issues.append(("error", _("Kode transaksi %s belum didukung AksaTax. Buat e-Faktur ini langsung di AksaTax.", code)))
        elif code in ("07", "08") and not self._coretax_keterangan_tambahan():
            issues.append(("error", _("Pilih keterangan tambahan untuk kode transaksi %s (tab Info Lainnya).", code)))
        partner = self.commercial_partner_id
        if not partner._coretax_identity():
            issues.append(("error", _("Pelanggan %s belum punya NPWP atau NIK.", partner.display_name)))
        elif not partner.with_company(self.company_id).coretax_counterparty_id:
            if connection and connection.auto_register_partner:
                issues.append(("warning", _("Pelanggan %s belum dicocokkan dengan AksaTax - Odoo akan mencarinya lewat "
                                            "NPWP, atau mendaftarkannya sebagai lawan transaksi baru, saat mengirim.",
                                            partner.display_name)))
            else:
                issues.append(("warning", _("Pelanggan %s belum dicocokkan dengan data di AksaTax - Odoo akan mencarinya "
                                            "otomatis lewat NPWP saat mengirim.", partner.display_name)))
        lines = self.invoice_line_ids.filtered(lambda l: l.display_type == "product" and l.tax_ids)
        no_code = lines.filtered(lambda l: not l.product_id.l10n_id_product_code)
        if no_code:
            issues.append(("warning", _("%s baris tanpa Kode Barang/Jasa Coretax (produk: %s) - akan memakai kode "
                                        "umum 000000.", len(no_code),
                                        ", ".join(no_code.mapped("product_id.display_name")[:3]) or _("tanpa produk"))))
        if hasattr(self, "_validate_tax_groups"):
            for message in self._validate_tax_groups() or []:
                issues.append(("error", message))
        try:
            self._coretax_line_vals()
        except (UserError, ValidationError) as exc:
            issues.append(("error", str(exc.args[0] if exc.args else exc)))
        if connection is not None and not connection:
            issues.insert(0, ("error", _("Koneksi AksaTax untuk perusahaan ini belum diatur.")))
        return issues

    # ── data yang dikirim ─────────────────────────────────────────────
    def _coretax_line_vals(self):
        """Baris e-Faktur memakai penyusun data e-Faktur bawaan Odoo
        (l10n_id_efaktur_coretax), supaya angkanya sama dengan XML Coretax."""
        self.ensure_one()
        vals = {}
        lines = self.invoice_line_ids.filtered(lambda l: l.display_type == "product" and l.tax_ids)
        for line in lines:
            line._l10n_id_coretax_build_invoice_line_vals(vals)
        result = []
        ChartTemplate = self.env["account.chart.template"].with_company(self.company_id)
        luxury_groups = {ChartTemplate.ref(xmlid, raise_if_not_found=False)
                         for xmlid in ("l10n_id_tax_group_luxury_goods", "l10n_id_tax_group_stlg")}
        luxury_groups.discard(False)
        for line, val in zip(lines, vals.get("lines", [])):
            unit = val.get("Unit") or line.product_uom_id.name or "UNIT"
            result.append({
                "kodeObjek": (val.get("Code") or "000000")[:20],
                "nama": (val.get("Name") or line.name or _("Barang/Jasa"))[:300],
                "satuan": unit[:30],
                "hargaSatuan": float(val["Price"]),
                "jumlahBarang": float(val["Qty"]),
                "diskon": float(val["TotalDiscount"]),
                "tarifPpnbm": float(val.get("STLGRate") or 0),
                "barangMewah": any(group in luxury_groups for group in line.tax_ids.tax_group_id),
                "keterangan": (line.name or "")[:300] if line.name and line.name != val.get("Name") else "",
            })
        return result

    def _coretax_keterangan_tambahan(self):
        """ID keterangan tambahan (mis. 'TD.00530' -> '30') untuk kode 07/08, '' bila tidak ada."""
        self.ensure_one()
        code = self.l10n_id_kode_transaksi
        if code not in ("07", "08"):
            return ""
        value = self["l10n_id_coretax_add_info_%s" % code]
        return str(int(value[-2:])) if value else ""

    def _coretax_payload(self, counterparty_id):
        self.ensure_one()
        payload = {
            "counterpartyId": counterparty_id,
            "kdJenisTransaksi": self.l10n_id_kode_transaksi or "01",
            "fgPengganti": "0",
            "fgUangMuka": "0",
            "tanggalFaktur": fields.Date.to_string(self.invoice_date),
            "referensi": (self.name or "")[:100],
            "items": self._coretax_line_vals(),
        }
        replacing = self._coretax_replacing()
        if replacing:
            payload["fgPengganti"] = "1"
            payload["nomorFakturDiganti"] = replacing.nomor_faktur
        keterangan = self._coretax_keterangan_tambahan()
        if keterangan:
            payload["idKeteranganTambahan"] = keterangan
        if self.ref:
            payload["nomorDokumenPendukung"] = self.ref[:100]
        return payload

    # ── pengiriman ────────────────────────────────────────────────────
    def _coretax_explain_error(self, exc):
        """AksaTax lama menolak kode transaksi 10 (atau keterangan tambahan
        baru untuk 07/08) sebagai VALIDATION; terjemahkan jadi pesan
        'perbarui server'."""
        self.ensure_one()
        kode = self.l10n_id_kode_transaksi
        if exc.code != "VALIDATION" or kode not in ("07", "08", "10"):
            return exc
        for detail in exc.details or []:
            if not isinstance(detail, dict):
                continue
            path = detail.get("path")
            if isinstance(path, (list, tuple)):
                path = ".".join(str(part) for part in path)
            if kode == "10" and path == "kdJenisTransaksi":
                return AksaTaxError(
                    _("Kode transaksi 10 butuh AksaTax versi terbaru. Minta admin AksaTax memperbarui server."),
                    code="NOT_SUPPORTED", status=exc.status, request_id=exc.request_id)
            if kode in ("07", "08") and path == "idKeteranganTambahan":
                return AksaTaxError(
                    _("Keterangan tambahan ini butuh AksaTax versi terbaru. Minta admin AksaTax memperbarui server."),
                    code="NOT_SUPPORTED", status=exc.status, request_id=exc.request_id)
        return exc

    def _coretax_create_faktur(self, mode="send", connection=None):
        """Buat e-Faktur di AksaTax untuk satu faktur, lalu (bila mode 'send')
        kirim ke DJP. Selalu menghasilkan dokumen coretax.faktur - yang gagal
        berstatus 'Gagal - perlu tindakan' dengan pesan yang bisa dibaca."""
        self.ensure_one()
        connection = connection or self.env["coretax.connection"]._for_company(self.company_id)
        errors = [msg for level, msg in self._coretax_check(connection) if level == "error"]
        if errors:
            raise UserError("\n".join(errors))
        replacing = self._coretax_replacing()
        previous = self.coretax_faktur_id
        if previous:
            previous.active = False
        version = max(self.with_context(active_test=False).coretax_faktur_ids.mapped("version") or [0]) + 1
        faktur = self.env["coretax.faktur"].create({"move_id": self.id, "version": version, "send_mode": mode,
                                                    "kd_jenis_transaksi": self.l10n_id_kode_transaksi,
                                                    "replaces_id": replacing.id})
        client = connection._client(faktur)
        try:
            partner = self.commercial_partner_id.with_company(self.company_id)
            counterparty = partner._coretax_counterparty_for(connection)
            if not counterparty:
                raise AksaTaxError(_(
                    "Pelanggan %s (NPWP %s) belum terdaftar di AksaTax. Klik 'Daftarkan ke AksaTax' di "
                    "Coretax › Data Master › Lawan Transaksi, lalu kirim ulang.",
                    partner.display_name, partner._coretax_identity() or "-"), code="COUNTERPARTY_MISSING")
            data = client.create_faktur(self._coretax_payload(counterparty), faktur._idempotency_base())
            faktur._apply_remote(data)
            if mode == "send":
                result = client.send_faktur(faktur.aksatax_id, f"{faktur._idempotency_base()}-kirim-1")
                faktur.write({"state": REMOTE_STATES.get(result.get("status"), "submitted"), "send_attempt": 1,
                              "submitted_at": fields.Datetime.now()})
        except AksaTaxError as caught:
            exc = self._coretax_explain_error(caught)
            if faktur.aksatax_id:
                # draft sudah ada di AksaTax, hanya kirim ke DJP yang gagal:
                # status tetap draft dan bisa dikirim ulang dari Odoo
                faktur.write({"last_error": exc.full_message()})
            else:
                faktur.write({"state": "error", "last_error": exc.full_message()})
            faktur.message_post(body=_("Gagal mengirim ke AksaTax: %s", exc.full_message()))
            faktur.error_code = exc.code or ""
            if self.env.context.get("coretax_auto"):
                faktur._schedule_fix_activity(_("e-Faktur gagal dikirim otomatis"))
            return faktur
        self.message_post(body=_("e-Faktur dibuat di AksaTax (%s).", dict(STATES).get(faktur.state)))
        return faktur

    def action_coretax_send(self):
        """Tombol 'Kirim e-Faktur' - buka wizard pemeriksaan & pengiriman."""
        moves = self.filtered(lambda m: m.move_type == "out_invoice")
        if not moves:
            raise UserError(_("Pilih minimal satu faktur pelanggan."))
        return {
            "type": "ir.actions.act_window", "res_model": "coretax.send.wizard", "view_mode": "form",
            "target": "new", "name": _("Kirim e-Faktur ke Coretax"),
            "context": {"default_move_ids": [(6, 0, moves.ids)]},
        }

    def action_coretax_refresh(self):
        self.coretax_faktur_id.action_refresh()
        return True

    def action_coretax_download_pdf(self):
        self.ensure_one()
        if not self.coretax_faktur_id:
            raise UserError(_("Faktur ini belum punya e-Faktur."))
        return self.coretax_faktur_id.action_download_pdf()

    def action_coretax_cancel_efaktur(self):
        self.ensure_one()
        if not self.coretax_faktur_id:
            raise UserError(_("Faktur ini belum punya e-Faktur."))
        return self.coretax_faktur_id.action_open_cancel_wizard()

    def action_coretax_request_replacement(self):
        self.ensure_one()
        if not self.coretax_faktur_id:
            raise UserError(_("Faktur ini belum punya e-Faktur."))
        return self.coretax_faktur_id.action_request_replacement()

    def action_coretax_view_faktur(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("coretax_integration.action_coretax_faktur")
        action["domain"] = [("move_id", "=", self.id)]
        action["context"] = {"active_test": False}
        if self.coretax_faktur_count == 1:
            action.update(views=[[False, "form"]], res_id=self.with_context(active_test=False).coretax_faktur_ids.id)
        return action

    def action_coretax_create_bupot(self):
        """Dari tagihan vendor: siapkan Bukti Potong PPh (draft)."""
        self.ensure_one()
        bill_date = self.invoice_date or fields.Date.context_today(self)
        return {
            "type": "ir.actions.act_window", "res_model": "coretax.bupot", "view_mode": "form", "target": "current",
            "name": _("Bukti Potong baru"),
            "context": {
                "default_move_id": self.id, "default_partner_id": self.commercial_partner_id.id,
                "default_jumlah_bruto": round(self.amount_untaxed_signed and abs(self.amount_untaxed_signed) or 0),
                "default_tanggal_pemotongan": fields.Date.to_string(bill_date),
                "default_dok_jenis": "02", "default_dok_nomor": (self.ref or self.name or "")[:100],
                "default_dok_tanggal": fields.Date.to_string(bill_date),
            },
        }

    # ── otomatis saat posting & pengaman reset ────────────────────────
    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        candidates = posted.filtered(lambda m: m.move_type == "out_invoice")
        if not candidates or self.env.context.get("coretax_no_auto"):
            return posted
        for company in candidates.company_id:
            connection = self.env["coretax.connection"]._for_company(company, raise_if_missing=False)
            if not connection or connection.auto_mode == "manual" or not connection.has_api_key:
                continue
            for move in candidates.filtered(lambda m: m.company_id == company and m.coretax_can_send):
                if any(level == "error" for level, _msg in move._coretax_check(connection)):
                    move.message_post(body=_("e-Faktur tidak dikirim otomatis: data belum lengkap. "
                                             "Klik 'Kirim e-Faktur' untuk melihat yang perlu diperbaiki."))
                    continue
                try:
                    with self.env.cr.savepoint():
                        move.with_context(coretax_auto=True)._coretax_create_faktur(
                            mode=connection.auto_mode, connection=connection)
                except Exception:  # pengiriman otomatis tidak boleh menggagalkan posting
                    _logger.exception("Kirim e-Faktur otomatis gagal untuk %s", move.name)
        return posted

    def _coretax_locking_faktur(self, allow_replacing=False):
        """e-Faktur yang mengunci faktur ini dari reset/batal, atau kosong."""
        self.ensure_one()
        replacing = self._coretax_replacing()
        if replacing and not allow_replacing:
            # e-Faktur lama masih berlaku di DJP selama penggantian dibuka, walau versi terakhir
            # (pengganti) ditolak/gagal - yang dijelaskan adalah e-Faktur lama itu.
            return replacing
        latest = self.sudo().coretax_faktur_id
        if self.coretax_state in LOCKING_STATES and not (allow_replacing and latest == replacing):
            return latest
        return self.env["coretax.faktur"]

    def _coretax_guard(self, action_label, allow_replacing=False):
        locks = [(move, move._coretax_locking_faktur(allow_replacing)) for move in self]
        locks = [(move, faktur) for move, faktur in locks if faktur]
        if not locks:
            return
        if allow_replacing:
            # reset ke draft: penggantian yang dibuka juga melepas kuncinya
            hint = _("Batalkan e-Faktur-nya dengan tombol Batalkan e-Faktur, atau buka penggantian dengan tombol "
                     "Ganti e-Faktur.")
        else:
            hint = _("Batalkan e-Faktur-nya dulu dengan tombol Batalkan e-Faktur.")
        raise UserError(_(
            "Faktur %(moves)s belum bisa %(action)s karena e-Faktur-nya masih berlaku di DJP (%(state)s). %(hint)s",
            moves=", ".join(move.name for move, _faktur in locks), action=action_label,
            state=dict(STATES).get(locks[0][1].state), hint=hint))

    # ── nota retur ────────────────────────────────────────────────────
    @api.depends("coretax_retur_ids", "coretax_retur_ids.state")
    def _compute_coretax_retur(self):
        for move in self:
            returs = move.sudo().coretax_retur_ids
            move.coretax_retur_count = len(returs)
            move.coretax_retur_live = any(r.state in RETUR_LIVE_STATES for r in returs)

    def _coretax_retur_guard(self, action_label):
        """Nota kredit dengan nota retur yang masih berlaku di AksaTax tidak boleh diubah."""
        refunds = self.filtered(lambda m: m.move_type in ("out_refund", "in_refund"))
        for move in refunds:
            live = move.sudo().coretax_retur_ids.filtered(lambda r: r.state in RETUR_LIVE_STATES)[:1]
            if live:
                raise UserError(_(
                    "Nota kredit ini punya nota retur %(nomor)s di AksaTax. Batalkan nota retur dulu, "
                    "lalu %(action)s.", nomor=live.nomor_dokumen_retur, action=action_label))

    def _coretax_retur_amounts(self):
        """Nilai retur dari nota kredit, dalam mata uang perusahaan: dict(dpp, ppn, ppnbm, tarif)."""
        self.ensure_one()
        stlg = self.env["account.chart.template"].with_company(self.company_id).ref(
            "l10n_id_tax_group_stlg", raise_if_not_found=False)
        ppn_taxes = self.line_ids.tax_ids.filtered(lambda t: t.amount > 0 and t.tax_group_id != stlg)
        ppnbm_taxes = self.line_ids.tax_ids.filtered(lambda t: t.tax_group_id == stlg) if stlg else self.env["account.tax"]
        product_lines = self.line_ids.filtered(lambda l: l.display_type == "product" and l.tax_ids & ppn_taxes)
        tax_lines = self.line_ids.filtered(lambda l: l.display_type == "tax")
        dpp = sum(abs(l.balance) for l in product_lines)
        ppn = abs(sum(tax_lines.filtered(lambda l: l.tax_line_id in ppn_taxes).mapped("balance")))
        ppnbm = abs(sum(tax_lines.filtered(lambda l: l.tax_line_id in ppnbm_taxes).mapped("balance")))
        return {"dpp": int(round(dpp)), "ppn": int(round(ppn)), "ppnbm": int(round(ppnbm)), "tarif": 12}

    def action_coretax_open_retur_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Kirim nota retur"), "res_model": "coretax.retur.wizard",
            "view_mode": "form", "target": "new",
            "context": {"active_model": "account.move", "active_id": self.id, "default_move_id": self.id},
        }

    def action_coretax_view_retur(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("coretax_integration.action_coretax_retur")
        action["domain"] = [("move_id", "=", self.id)]
        if self.coretax_retur_count == 1:
            action.update(views=[[False, "form"]], res_id=self.sudo().coretax_retur_ids.id)
        return action

    def button_draft(self):
        self._coretax_guard(_("dikembalikan ke draft"), allow_replacing=True)
        self._coretax_retur_guard(_("kembalikan ke draft"))
        return super().button_draft()

    def button_cancel(self):
        self._coretax_guard(_("dibatalkan"))
        self._coretax_retur_guard(_("batalkan nota kredit"))
        return super().button_cancel()

    def unlink(self):
        self._coretax_retur_guard(_("hapus nota kredit"))
        returs = self.filtered(lambda m: m.move_type in ("out_refund", "in_refund")).sudo().coretax_retur_ids
        # Nota retur yang tidak pernah sampai ke AksaTax ikut dihapus; yang punya riwayat menahan penghapusan.
        returs.filtered(lambda r: not r.aksatax_id).unlink()
        history = returs.exists()
        if history:
            raise UserError(_("Nota kredit ini punya riwayat nota retur %s di AksaTax, jadi tidak bisa dihapus. "
                              "Batalkan nota kredit saja.", history[0].nomor_dokumen_retur))
        return super().unlink()
