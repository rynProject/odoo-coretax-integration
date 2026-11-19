from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import frozendict
from odoo.tools.sql import column_exists, create_column


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    coretax_tax_object_id = fields.Many2one(
        "coretax.tax.object", string="Kode objek PPh (Coretax)",
        compute="_compute_coretax_tax_object_id", store=True, readonly=False, copy=True, index="btree_not_null",
        help="Kode objek pajak PPh baris ini. Terisi otomatis dari pajak PPh pada baris; "
             "dapat diganti bila kode objek berbeda.")

    def _auto_init(self):
        # kolom dibuat langsung (kosong) agar upgrade tidak menghitung ulang seluruh journal item;
        # pajak PPh belum ada sebelum versi ini, jadi baris lama memang kosong.
        if not column_exists(self.env.cr, "account_move_line", "coretax_tax_object_id"):
            create_column(self.env.cr, "account_move_line", "coretax_tax_object_id", "int4")
        return super()._auto_init()

    def copy_data(self, default=None):
        # nilai False eksplisit (baris lama sebelum pajaknya diberi kode objek) jangan disalin,
        # agar compute mengisinya dari pajak.
        vals_list = super().copy_data(default)
        for vals in vals_list:
            if "coretax_tax_object_id" in vals and not vals["coretax_tax_object_id"]:
                del vals["coretax_tax_object_id"]
        return vals_list

    @api.depends("tax_ids")
    def _compute_coretax_tax_object_id(self):
        for line in self:
            if line.display_type == "product":
                pph = line.tax_ids.filtered("coretax_tax_object_id")[:1]
                line.coretax_tax_object_id = pph.coretax_tax_object_id
            else:
                # journal item pajak menerima nilainya lewat grouping key; jangan dikosongkan
                line.coretax_tax_object_id = line.coretax_tax_object_id

    # ── journal item PPh per kode objek (mesin pajak Odoo 16) ──────────
    # Kunci pengelompokan journal item pajak (compute_all_tax di baris dasar, tax_key di baris pajak) diberi kode
    # objek PPh, sehingga satu pajak PPh dengan kode objek berbeda menjadi journal item terpisah. Kunci yang sama
    # menjadi nilai journal item pajak baru, jadi kode objeknya ikut tersimpan. Pajak selain PPh memakai False.
    @api.depends("coretax_tax_object_id")
    def _compute_all_tax(self):
        super()._compute_all_tax()
        Repartition = self.env["account.tax.repartition.line"]
        for line in self:
            if not line.compute_all_tax:
                continue
            values = {}
            for key, vals in line.compute_all_tax.items():
                if "tax_repartition_line_id" in key:
                    tax_object = False
                    tax = Repartition.browse(key["tax_repartition_line_id"]).tax_id
                    if tax.coretax_tax_object_id:
                        tax_object = (line.coretax_tax_object_id or tax.coretax_tax_object_id).id
                    key = frozendict({**key, "coretax_tax_object_id": tax_object})
                values[key] = vals
            line.compute_all_tax = values

    @api.depends("coretax_tax_object_id")
    def _compute_tax_key(self):
        super()._compute_tax_key()
        for line in self.filtered("tax_repartition_line_id"):
            line.tax_key = frozendict({**line.tax_key, "coretax_tax_object_id": line.coretax_tax_object_id.id or False})

    @api.constrains("tax_ids", "coretax_tax_object_id")
    def _check_coretax_pph_line(self):
        for line in self.filtered(lambda l: l.display_type == "product"):
            pph_taxes = line.tax_ids.filtered("coretax_tax_object_id")
            if len(pph_taxes) > 1:
                raise ValidationError(_("Satu baris hanya boleh memakai satu pajak PPh."))
            if pph_taxes and not line.coretax_tax_object_id:
                raise ValidationError(_(
                    "Baris dengan pajak PPh wajib punya kode objek pajak. "
                    "Pilih kode objek di kolom Kode objek."))
            if pph_taxes and line.coretax_tax_object_id \
                    and line.coretax_tax_object_id.jenis != pph_taxes.coretax_tax_object_id.jenis:
                raise ValidationError(_(
                    "Jenis kode objek pajak pada baris '%(line)s' harus sama dengan jenis kode objek pajak PPh-nya.",
                    line=line.name or line.display_name))
