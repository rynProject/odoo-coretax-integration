from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
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
