import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.coretax_retur import ITEM_VALUES, KELUARAN_MESSAGE, LIVE_STATES
from ..tools.client import AksaTaxError

# Derau pecahan saat membandingkan nilai retur dengan sisa (bukan toleransi rupiah).
NOISE = 1e-6


def _digits(value):
    return re.sub(r"\D", "", value or "")


def _faktur_key(value):
    """Nomor faktur untuk dicocokkan: 16 digit format lama -> 13 digit NSFP, 13/17 digit apa adanya."""
    digits = _digits(value)
    return digits[3:] if len(digits) == 16 else digits


class CoretaxReturWizard(models.TransientModel):
    """Kirim nota retur dari nota kredit vendor yang sudah diposting.

    PajakMu meretur faktur masukan yang sudah ada di sana (tarikan dari DJP,
    berstatus Dikreditkan / Tidak dikreditkan), per barang/jasa. Retur atas
    faktur keluaran dibuat pembeli di Coretax, jadi tidak dikirim dari Odoo."""

    _name = "coretax.retur.wizard"
    _description = "Kirim nota retur"

    move_id = fields.Many2one("account.move", string="Nota kredit", required=True, ondelete="cascade")
    sisi = fields.Selection([("keluaran", "Keluaran"), ("masukan", "Masukan")], compute="_compute_sisi")
    company_id = fields.Many2one(related="move_id.company_id")
    currency_id = fields.Many2one(related="move_id.company_id.currency_id")
    nomor_dokumen_retur = fields.Char(string="Nomor dokumen retur",
                                      help="Kosongkan agar PajakMu memberi nomor register retur.")
    tanggal_retur = fields.Date(string="Tanggal retur", required=True)
    referensi = fields.Char(string="Referensi")
    nomor_faktur = fields.Char(string="Nomor Faktur Pajak vendor",
                               help="Nomor faktur pajak dari vendor; dipakai untuk mencari faktur masukan di PajakMu.")
    faktur_masukan_id = fields.Char(string="ID faktur masukan PajakMu", readonly=True)
    faktur_masukan_nomor = fields.Char(string="Nomor faktur masukan", readonly=True)
    faktur_masukan_info = fields.Char(string="Faktur masukan", readonly=True)
    lookup_message = fields.Text(string="Keterangan pencarian", readonly=True)
    is_creditable = fields.Boolean(string="Retur dapat dikreditkan", default=True,
                                   help="Centang bila PPN faktur masukan asal dikreditkan.")
    line_ids = fields.One2many("coretax.retur.wizard.line", "wizard_id", string="Barang/jasa")
    total_dpp = fields.Monetary(string="Total DPP retur", compute="_compute_totals")
    total_ppn = fields.Monetary(string="Total PPN retur", compute="_compute_totals")
    total_ppnbm = fields.Monetary(string="Total PPnBM retur", compute="_compute_totals")

    @api.depends("move_id.move_type")
    def _compute_sisi(self):
        for wiz in self:
            wiz.sisi = "masukan" if wiz.move_id.move_type == "in_refund" else "keluaran"

    @api.depends("line_ids.retur_dpp", "line_ids.retur_ppn", "line_ids.retur_ppnbm")
    def _compute_totals(self):
        for wiz in self:
            wiz.total_dpp = sum(wiz.line_ids.mapped("retur_dpp"))
            wiz.total_ppn = sum(wiz.line_ids.mapped("retur_ppn"))
            wiz.total_ppnbm = sum(wiz.line_ids.mapped("retur_ppnbm"))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        move_id = res.get("move_id") or (self.env.context.get("active_id")
                                         if self.env.context.get("active_model") == "account.move" else False)
        move = self.env["account.move"].browse(move_id).exists()
        if not move:
            return res
        res.update({
            "move_id": move.id, "nomor_dokumen_retur": move.name,
            "tanggal_retur": move.invoice_date or move.date, "referensi": move.name,
        })
        if move.move_type == "in_refund":
            res["nomor_faktur"] = move.reversed_entry_id.coretax_vendor_tax_number or False
            if res["nomor_faktur"] and move.state == "posted":
                res.update(self._lookup_values(move, res["nomor_faktur"]))
        return res

    # ── faktur masukan di PajakMu ─────────────────────────────────────
    def _lookup_values(self, move, nomor_faktur):
        """Cari faktur masukan di PajakMu lalu muat barang/jasanya: dict nilai wizard."""
        empty = {"faktur_masukan_id": False, "faktur_masukan_nomor": False, "faktur_masukan_info": False,
                 "line_ids": []}
        connection = self.env["coretax.connection"]._for_company(move.company_id, raise_if_missing=False)
        if not connection:
            return dict(empty, lookup_message=_("Koneksi PajakMu untuk perusahaan ini belum diatur."))
        client = connection._client(move)
        key = _faktur_key(nomor_faktur)
        try:
            rows = client.retur_faktur_masukan(query=key)
            match = next((r for r in rows if _faktur_key(r.get("nomorFaktur")) == key
                          or _digits(r.get("nomorFaktur")).endswith(key)), None)
            if not match:
                return dict(empty, lookup_message=self._not_found_message(client, move, nomor_faktur))
            detail = client.retur_faktur_masukan_items(match["id"])
        except AksaTaxError as exc:
            return dict(empty, lookup_message=exc.full_message())
        lines = self._lines_from_detail(detail)
        self._prefill_from_credit_note(move, lines)
        info = " · ".join(part for part in (match.get("nomorFaktur"), match.get("namaPenjual"),
                                             (match.get("status") or "").replace("_", " ").title()) if part)
        return {
            "faktur_masukan_id": match["id"], "faktur_masukan_nomor": match.get("nomorFaktur") or False,
            "faktur_masukan_info": info, "lookup_message": False,
            "is_creditable": match.get("status") == "DIKREDITKAN",
            "line_ids": [(0, 0, line) for line in lines],
        }

    def _not_found_message(self, client, move, nomor_faktur):
        message = _("Faktur masukan %s tidak ditemukan di PajakMu atau sudah habis diretur. Pastikan faktur sudah "
                    "ditarik dari DJP dan berstatus Dikreditkan / Tidak dikreditkan di PajakMu, lalu klik Cari ulang.",
                    nomor_faktur)
        npwp = _digits(move.commercial_partner_id.vat)
        if not npwp:
            return message
        try:
            candidates = client.retur_faktur_masukan(query=npwp)
        except AksaTaxError:
            return message
        if candidates:
            nomor = ", ".join(c.get("nomorFaktur") or "-" for c in candidates[:5])
            message += "\n" + _("Faktur masukan vendor ini yang bisa diretur: %s", nomor)
        return message

    @api.model
    def _lines_from_detail(self, detail):
        """Baris wizard dari GET nota-retur/faktur-masukan/{id}/items: nilai asal dan sisa yang bisa diretur."""
        items = detail.get("items") or []
        done = detail.get("sudahDiretur") or []
        lines = []
        for index, item in enumerate(items):
            already = done[index] if index < len(done) and isinstance(done[index], dict) else {}
            line = {"index": index, "nama": item.get("namaBrgJasa") or "", "satuan": item.get("satuanBrgJasa") or ""}
            for field, key, origin in ITEM_VALUES:
                total = float(item.get(origin) or 0)
                line[f"asal_{field[6:]}"] = total
                line[f"sisa_{field[6:]}"] = max(0.0, round(total - float(already.get(key) or 0), 2))
                line[field] = 0.0
            lines.append(line)
        return lines

    @api.model
    def _prefill_from_credit_note(self, move, lines):
        """Isi awal nilai retur dari nota kredit: baris yang namanya sama dengan barang/jasa DJP memakai nilai
        barisnya; selain itu dibagi sebanding sisa DPP. Pengguna tetap bisa mengubahnya."""
        product_lines = move.line_ids.filtered(lambda l: l.display_type == "product" and l.balance)
        free = {i: line for i, line in enumerate(lines)}
        matched = []
        for aml in product_lines:
            names = {(aml.product_id.name or "").strip().casefold(), (aml.name or "").strip().casefold()} - {""}
            hit = next((i for i, line in free.items() if line["nama"].strip().casefold() in names), None)
            if hit is None:
                matched = None
                break
            matched.append((free.pop(hit), aml))
        if matched:
            for line, aml in matched:
                dpp = min(abs(aml.balance), line["sisa_dpp"])
                ratio = dpp / line["asal_dpp"] if line["asal_dpp"] else 0
                for field, _key, _origin in ITEM_VALUES:
                    part = field[6:]
                    line[field] = min(round(line[f"asal_{part}"] * ratio, 2), line[f"sisa_{part}"])
                line["retur_dpp"] = round(dpp, 2)
                line["retur_jumlah"] = min(abs(aml.quantity), line["sisa_jumlah"])
            return
        remaining = sum(line["sisa_dpp"] for line in lines)
        credit = move._coretax_retur_amounts()["dpp"]
        ratio = min(1.0, credit / remaining) if remaining else 0
        for line in lines:
            for field, _key, _origin in ITEM_VALUES:
                line[field] = round(line[f"sisa_{field[6:]}"] * ratio, 2)

    def action_lookup(self):
        """Tombol Cari ulang: cari faktur masukan dengan nomor yang tertulis di wizard."""
        self.ensure_one()
        if not self.nomor_faktur:
            raise UserError(_("Isi Nomor Faktur Pajak vendor dulu."))
        vals = self._lookup_values(self.move_id, self.nomor_faktur)
        vals["line_ids"] = [(5, 0, 0)] + vals["line_ids"]
        self.write(vals)
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "views": [[False, "form"]], "target": "new", "name": _("Kirim nota retur")}

    # ── kirim ─────────────────────────────────────────────────────────
    def _check_preconditions(self):
        move = self.move_id
        if move.move_type == "out_refund":
            raise UserError(_(KELUARAN_MESSAGE))
        if move.move_type != "in_refund" or move.state != "posted":
            raise UserError(_("Posting nota kredit vendor dulu, lalu kirim nota retur."))
        if not move.reversed_entry_id:
            raise UserError(_("Nota kredit ini tidak terhubung ke tagihan asal. Buat nota kredit lewat tombol "
                              "Tambah Nota Kredit di tagihan asal."))
        live = move.sudo().coretax_retur_ids.filtered(lambda r: r.state in LIVE_STATES)[:1]
        if live:
            raise UserError(_("Nota kredit ini sudah punya nota retur %s. Buka lewat tombol Nota Retur.",
                              live.nomor_dokumen_retur))
        if not self.faktur_masukan_id:
            raise UserError(self.lookup_message or _("Faktur masukan asal belum ditemukan di PajakMu. Isi Nomor "
                                                     "Faktur Pajak vendor lalu klik Cari ulang."))
        problems = []
        for line in self.line_ids:
            for field, _key, _origin in ITEM_VALUES:
                part = field[6:]
                if line[field] < 0:
                    problems.append(_("%(nama)s: nilai retur tidak boleh negatif.", nama=line.nama))
                elif line[field] > line[f"sisa_{part}"] + NOISE:
                    problems.append(_("%(nama)s: %(label)s retur melebihi sisa faktur asal (%(sisa)s).",
                                      nama=line.nama, label=line._fields[field].string, sisa=line[f"sisa_{part}"]))
        if not any(line.retur_dpp > 0 for line in self.line_ids):
            problems.append(_("DPP retur harus lebih dari 0 pada minimal satu barang/jasa."))
        if problems:
            raise UserError("\n".join(problems))

    def action_confirm(self):
        self.ensure_one()
        self._check_preconditions()
        move = self.move_id
        lines = [(0, 0, {"index": line.index, "nama": line.nama,
                         **{field: line[field] for field, _key, _origin in ITEM_VALUES}})
                 for line in self.line_ids if any(line[field] for field, _key, _origin in ITEM_VALUES)]
        vals = {
            "sisi": "masukan",
            "move_id": move.id,
            "faktur_id": False,
            "faktur_masukan_id": self.faktur_masukan_id,
            "is_creditable": self.is_creditable,
            "nomor_faktur": self.faktur_masukan_nomor or self.nomor_faktur,
            "partner_id": move.commercial_partner_id.id,
            "nomor_dokumen_retur": self.nomor_dokumen_retur or move.name,
            "tanggal_retur": self.tanggal_retur,
            "referensi": self.referensi or move.name,
            "line_ids": [(5, 0, 0)] + lines,
        }
        # nota retur yang belum sampai ke PajakMu (baru/gagal) dipakai ulang, bukan dibuat ganda
        retur = move.sudo().coretax_retur_ids.filtered(lambda r: r.state in ("new", "error") and not r.aksatax_id)[:1]
        if retur:
            retur.write(vals)
        else:
            retur = self.env["coretax.retur"].create(vals)
        retur.action_send()
        return {
            "type": "ir.actions.act_window", "res_model": "coretax.retur", "res_id": retur.id,
            "view_mode": "form", "views": [[False, "form"]],
        }


class CoretaxReturWizardLine(models.TransientModel):
    _name = "coretax.retur.wizard.line"
    _description = "Barang/jasa nota retur (wizard)"
    _order = "index"

    wizard_id = fields.Many2one("coretax.retur.wizard", required=True, ondelete="cascade")
    index = fields.Integer(string="No.")
    nama = fields.Char(string="Barang/jasa", readonly=True)
    satuan = fields.Char(string="Satuan", readonly=True)
    asal_jumlah = fields.Float(string="Jumlah asal", digits=(16, 2), readonly=True)
    asal_diskon = fields.Float(string="Diskon asal", digits=(16, 2), readonly=True)
    asal_dpp = fields.Float(string="DPP asal", digits=(16, 2), readonly=True)
    asal_dpp_lain = fields.Float(string="DPP nilai lain asal", digits=(16, 2), readonly=True)
    asal_ppn = fields.Float(string="PPN asal", digits=(16, 2), readonly=True)
    asal_ppnbm = fields.Float(string="PPnBM asal", digits=(16, 2), readonly=True)
    sisa_jumlah = fields.Float(string="Sisa jumlah", digits=(16, 2), readonly=True)
    sisa_diskon = fields.Float(string="Sisa diskon", digits=(16, 2), readonly=True)
    sisa_dpp = fields.Float(string="Sisa DPP", digits=(16, 2), readonly=True)
    sisa_dpp_lain = fields.Float(string="Sisa DPP nilai lain", digits=(16, 2), readonly=True)
    sisa_ppn = fields.Float(string="Sisa PPN", digits=(16, 2), readonly=True)
    sisa_ppnbm = fields.Float(string="Sisa PPnBM", digits=(16, 2), readonly=True)
    retur_jumlah = fields.Float(string="Jumlah", digits=(16, 2))
    retur_diskon = fields.Float(string="Diskon", digits=(16, 2))
    retur_dpp = fields.Float(string="DPP", digits=(16, 2))
    retur_dpp_lain = fields.Float(string="DPP nilai lain", digits=(16, 2))
    retur_ppn = fields.Float(string="PPN", digits=(16, 2))
    retur_ppnbm = fields.Float(string="PPnBM", digits=(16, 2))
