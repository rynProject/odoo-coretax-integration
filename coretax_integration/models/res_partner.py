import hashlib
import json
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.client import AksaTaxError
from ..tools.countries import iso3
from .coretax_connection import DEFAULT_PARTNER_MAPPING


def digits(value):
    return re.sub(r"\D", "", value or "")


# Jenis identitas pembeli (l10n_id_efaktur_coretax) -> jenisId lawan transaksi AksaTax.
JENIS_ID = {"NIK": "NIK", "Passport": "PASPOR", "Other": "LAINNYA"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Satu NPWP di AksaTax = satu tenant, jadi tautannya per perusahaan Odoo.
    coretax_counterparty_id = fields.Char(string="ID lawan transaksi AksaTax", company_dependent=True, copy=False,
                                          help="Diisi otomatis saat dicocokkan dengan data Lawan Transaksi di AksaTax.")
    coretax_counterparty_name = fields.Char(string="Nama di AksaTax", company_dependent=True, copy=False)
    coretax_vswp_status = fields.Char(string="Status VSWP", company_dependent=True, copy=False,
                                      help="Hasil verifikasi NPWP di AksaTax (VSWP).")
    coretax_match_state = fields.Selection([("matched", "Sudah cocok"), ("unmatched", "Belum dicocokkan"),
                                            ("no_id", "Belum ada NPWP/NIK")],
                                           string="Status AksaTax", compute="_compute_coretax_match_state",
                                           search="_search_coretax_match_state")

    @api.depends_context("company")
    @api.depends("vat", "l10n_id_nik", "l10n_id_buyer_document_number", "l10n_id_buyer_document_type")
    def _compute_coretax_match_state(self):
        connection = self._coretax_connection()
        for partner in self:
            if partner.coretax_counterparty_id:
                partner.coretax_match_state = "matched"
            elif partner._coretax_identity(connection):
                partner.coretax_match_state = "unmatched"
            else:
                partner.coretax_match_state = "no_id"

    def _search_coretax_match_state(self, operator, value):
        if operator not in ("=", "!=") or value not in ("matched", "unmatched", "no_id"):
            raise UserError(_("Pencarian status AksaTax ini tidak didukung."))
        has_id = ["|", "|"] + [(name, "!=", False) for name in self._coretax_identity_fields()]
        domain = {
            "matched": [("coretax_counterparty_id", "!=", False)],
            "unmatched": [("coretax_counterparty_id", "=", False)] + has_id,
            "no_id": [("coretax_counterparty_id", "=", False)] + [(n, "=", False) for n in self._coretax_identity_fields()],
        }[value]
        return domain if operator == "=" else ["!"] + domain

    # ── pemetaan field (Coretax › Pengaturan › Koneksi AksaTax) ───────
    def _coretax_connection(self):
        return self.env["coretax.connection"]._for_company(self.env.company, raise_if_missing=False)

    def _coretax_value(self, key, connection=None):
        """Nilai satu data lawan transaksi dari field kontak yang dipetakan, sebagai teks."""
        self.ensure_one()
        connection = connection if connection is not None else self._coretax_connection()
        name = connection.sudo()._partner_field_name(key) if connection else DEFAULT_PARTNER_MAPPING[key]
        if name == "contact_address":
            # alamat bawaan disusun sendiri: contact_address Odoo ikut memuat nama perusahaan dan negara
            return ", ".join(part for part in (self.street, self.street2, self.city, self.state_id.name, self.zip) if part)
        if name not in self._fields:
            return ""
        value = self[name]
        if isinstance(value, models.BaseModel):
            return value.display_name or ""
        return str(value) if value not in (False, None) else ""

    def _coretax_identity_fields(self):
        """Field kontak tersimpan yang berisi NPWP/NIK/nomor identitas (untuk domain pencarian)."""
        connection = self._coretax_connection()
        result = []
        for key in ("npwp", "nik", "doc_number"):
            name = connection.sudo()._partner_field_name(key) if connection else DEFAULT_PARTNER_MAPPING[key]
            stored = name in self._fields and self._fields[name].store
            result.append(name if stored else DEFAULT_PARTNER_MAPPING[key])
        return result

    def _coretax_kind(self, connection=None):
        """NPWP / NIK / PASPOR / LAINNYA menurut jenis identitas di Odoo."""
        self.ensure_one()
        kind = JENIS_ID.get(self.l10n_id_buyer_document_type)
        if kind:
            return kind
        if digits(self._coretax_value("npwp", connection)):
            return "NPWP"
        return "NIK" if digits(self._coretax_value("nik", connection)) else "NPWP"

    def _coretax_identity(self, connection=None):
        """NPWP (atau NIK/nomor dokumen) kontak, hanya angka."""
        self.ensure_one()
        kind = self._coretax_kind(connection)
        if kind == "NPWP":
            return digits(self._coretax_value("npwp", connection))
        if kind == "NIK":
            return digits(self._coretax_value("nik", connection) or self._coretax_value("doc_number", connection))
        return digits(self._coretax_value("doc_number", connection))

    def _coretax_npwp_variants(self, connection=None):
        """NPWP 15 digit (lama) dan 16 digit (Coretax) - AksaTax menyimpan
        salah satunya, jadi keduanya dicoba."""
        npwp = digits(self._coretax_value("npwp", connection))
        variants = [npwp] if npwp else []
        if len(npwp) == 15:
            variants.append("0" + npwp)
        elif len(npwp) == 16 and npwp.startswith("0"):
            variants.append(npwp[1:])
        return variants

    @api.model
    def _coretax_unmatched_domain(self):
        npwp_field = self._coretax_identity_fields()[0]
        return [("is_company", "=", True), (npwp_field, "!=", False), ("coretax_counterparty_id", "=", False),
                ("customer_rank", ">", 0)]

    def _coretax_set_counterparty(self, row):
        self.ensure_one()
        self.write({
            "coretax_counterparty_id": row.get("id"),
            "coretax_counterparty_name": row.get("nama") or False,
            "coretax_vswp_status": row.get("vswpStatus") or False,
        })

    def _coretax_auto_match(self, connection):
        """Cari lawan transaksi di AksaTax lewat NPWP; dipakai otomatis bila
        hasilnya tepat satu. Mengembalikan ID AksaTax atau False."""
        self.ensure_one()
        client = connection._client(self)
        for npwp in self._coretax_npwp_variants(connection):
            rows = client.find_counterparties(npwp=npwp)
            if len(rows) == 1:
                self._coretax_set_counterparty(rows[0])
                return rows[0]["id"]
        return False

    # ── mendaftarkan sebagai lawan transaksi AksaTax ──────────────────
    def _coretax_counterparty_payload(self, connection):
        """Data lawan transaksi untuk POST /public/lawan-transaksi (skema counterpartySchema AksaTax)."""
        self.ensure_one()
        kind = self._coretax_kind(connection)
        npwp = digits(self._coretax_value("npwp", connection)) if kind == "NPWP" else ""
        tku = digits(self._coretax_value("tku", connection))
        if len(tku) == 6 and len(npwp) == 16:
            tku = npwp + tku
        address_lines = (self._coretax_value("address", connection) or "").splitlines()
        alamat = ", ".join(" ".join(line.split()) for line in address_lines if line.strip())
        return {
            "jenisId": kind,
            "npwp": npwp,
            "nik": self._coretax_identity(connection) if kind == "NIK" else "",
            "nomorIdLain": self._coretax_value("doc_number", connection).strip() if kind in ("PASPOR", "LAINNYA") else "",
            "nitku": tku if len(tku) == 22 else "",
            "nama": " ".join(self._coretax_value("name", connection).split())[:200],
            "alamat": alamat[:500],
            "email": self._coretax_value("email", connection).strip().lower(),
            "negara": iso3(self.country_id),
        }

    def _coretax_register_check(self, connection):
        """Masalah data yang membuat kontak belum bisa didaftarkan (daftar kosong = siap)."""
        self.ensure_one()
        payload = self._coretax_counterparty_payload(connection)
        problems = []
        if payload["jenisId"] == "NPWP" and len(payload["npwp"]) not in (15, 16):
            problems.append(_("NPWP belum diisi atau tidak 15/16 digit."))
        if payload["jenisId"] == "NIK" and len(payload["nik"]) != 16:
            problems.append(_("NIK belum diisi atau tidak 16 digit."))
        if payload["jenisId"] in ("PASPOR", "LAINNYA") and not payload["nomorIdLain"]:
            problems.append(_("Nomor paspor/identitas belum diisi."))
        if len(payload["nama"]) < 2:
            problems.append(_("Nama minimal 2 karakter."))
        if len(payload["alamat"]) < 3:
            problems.append(_("Alamat belum diisi."))
        if payload["email"] and not EMAIL_RE.match(payload["email"]):
            problems.append(_("Email %s tidak valid.", payload["email"]))
        return problems

    def _coretax_register(self, connection):
        """Daftarkan kontak ke AksaTax lalu tautkan. 'created' = lawan transaksi baru,
        'linked' = NPWP sudah terdaftar, ditautkan ke yang ada. Galat lain: AksaTaxError."""
        self.ensure_one()
        payload = self._coretax_counterparty_payload(connection)
        client = connection._client(self)
        uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid", "odoo")
        # kunci stabil per kontak + isi, supaya kiriman ulang tidak membuat data ganda
        content = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
        try:
            row = client.create_counterparty(payload, f"odoo-{uuid}-partner-{self.id}-{content}")
        except AksaTaxError as exc:
            if exc.code != "COUNTERPARTY_EXISTS":
                raise
            existing_id = exc.data.get("id")
            rows = [r for r in client.find_counterparties(npwp=payload["npwp"]) if r.get("id") == existing_id] \
                if payload["npwp"] else []
            self._coretax_set_counterparty(rows[0] if rows else {"id": existing_id})
            self.message_post(body=_("NPWP sudah terdaftar di AksaTax; ditautkan ke lawan transaksi %s.",
                                     self.coretax_counterparty_name or existing_id))
            return "linked"
        self._coretax_set_counterparty(row)
        self.message_post(body=_("Didaftarkan ke AksaTax sebagai lawan transaksi %s.", row.get("nama") or self.name))
        return "created"

    def _coretax_counterparty_for(self, connection):
        """ID lawan transaksi AksaTax untuk kontak ini: yang sudah tertaut, dicari lewat NPWP,
        atau didaftarkan otomatis (bila diaktifkan di koneksi). False bila tidak ada."""
        self.ensure_one()
        if self.coretax_counterparty_id:
            return self.coretax_counterparty_id
        if self._coretax_auto_match(connection):
            return self.coretax_counterparty_id
        if connection.auto_register_partner:
            problems = self._coretax_register_check(connection)
            if problems:
                raise AksaTaxError(_(
                    "%(partner)s belum terdaftar di AksaTax dan datanya belum lengkap untuk didaftarkan otomatis: "
                    "%(problems)s Lengkapi data kontak, lalu kirim ulang.",
                    partner=self.display_name, problems=" ".join(problems)), code="COUNTERPARTY_INCOMPLETE")
            self._coretax_register(connection)
            return self.coretax_counterparty_id
        return False

    # ── tombol ────────────────────────────────────────────────────────
    def action_coretax_register(self):
        partners = self.commercial_partner_id
        if not partners:
            raise UserError(_("Pilih minimal satu kontak."))
        return {
            "type": "ir.actions.act_window", "res_model": "coretax.partner.register.wizard", "view_mode": "form",
            "target": "new", "name": _("Daftarkan ke AksaTax"),
            "context": {"default_partner_ids": [(6, 0, partners.ids)]},
        }

    def action_coretax_match_all(self):
        """Cocokkan massal lewat NPWP: kontak terpilih (atau semua pelanggan
        ber-NPWP yang belum cocok bila dipanggil tanpa record). Yang hasilnya
        tidak tepat satu dilewati - bisa dicocokkan manual atau didaftarkan."""
        connection = self.env["coretax.connection"]._for_company(self.env.company)
        partners = self.filtered(lambda p: p._coretax_identity() and not p.coretax_counterparty_id) \
            if self else self.search(self._coretax_unmatched_domain())
        matched, skipped = 0, []
        for partner in partners.filtered("is_company"):
            try:
                if partner.commercial_partner_id._coretax_auto_match(connection):
                    matched += 1
                else:
                    skipped.append(partner.display_name)
            except AksaTaxError as exc:
                raise UserError(exc.full_message()) from exc
        message = _("%s kontak berhasil dicocokkan.", matched)
        if skipped:
            message += _(" Belum ditemukan/ganda di AksaTax (%s): %s%s. Klik Daftarkan ke AksaTax untuk mendaftarkannya.",
                         len(skipped), ", ".join(skipped[:5]), "…" if len(skipped) > 5 else "")
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Pencocokan AksaTax"), "message": message, "sticky": bool(skipped),
                       "type": "success" if not skipped else "warning",
                       "next": {"type": "ir.actions.client", "tag": "soft_reload"}},
        }

    def action_coretax_match(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "res_model": "coretax.partner.match.wizard", "view_mode": "form",
            "target": "new", "name": _("Cocokkan dengan AksaTax"),
            "context": {"default_partner_id": self.commercial_partner_id.id},
        }

    def action_coretax_unlink(self):
        self.write({"coretax_counterparty_id": False, "coretax_counterparty_name": False, "coretax_vswp_status": False})
        return True
