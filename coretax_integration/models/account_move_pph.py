from collections import defaultdict

from odoo import _, fields, models
from odoo.exceptions import UserError

from .coretax_bupot import EDITABLE_STATES, INACTIVE_STATES


class AccountMove(models.Model):
    _inherit = "account.move"

    coretax_bupot_ids = fields.One2many("coretax.bupot", "move_id", string="Bukti potong PPh")

    def _get_invoice_counterpart_amls_for_early_payment_discount_per_payment_term_line(self):
        # EXTENDS 'account': jumlahkan pajak per repartition line, bukan per kode objek PPh,
        # supaya selisih diskon pembayaran dini PPh tidak saling menimpa.
        return super(AccountMove, self.with_context(coretax_no_pph_split=True)) \
            ._get_invoice_counterpart_amls_for_early_payment_discount_per_payment_term_line()

    def _coretax_pph_family(self):
        """Tagihan ini beserta nota debit/kredit pembelian (in_refund) terposting yang membaliknya."""
        self.ensure_one()
        return self | self.reversal_move_ids.filtered(lambda m: m.move_type == "in_refund" and m.state == "posted")

    def _coretax_pph_groups(self):
        """{kode objek: baris produk ber-PPh} dari tagihan dan nota kreditnya."""
        self.ensure_one()
        groups = defaultdict(lambda: self.env["account.move.line"])
        for line in self._coretax_pph_family().line_ids:
            if line.display_type == "product" and line.tax_ids.filtered("coretax_tax_object_id") \
                    and line.coretax_tax_object_id:
                groups[line.coretax_tax_object_id] |= line
        return dict(groups)

    def _coretax_sync_bupot(self):
        """Samakan bukti potong otomatis (satu per kode objek) dengan baris PPh tagihan terposting."""
        Bupot = self.env["coretax.bupot"].with_context(coretax_bupot_sync=True)
        for bill in self.filtered(lambda m: m.state == "posted"):
            groups = bill._coretax_pph_groups()
            rates = {}
            for tax_object, lines in groups.items():
                rates[tax_object] = sorted({abs(t.amount) for t in lines.tax_ids.filtered("coretax_tax_object_id")})
                if len(rates[tax_object]) > 1:
                    raise UserError(_(
                        "Baris dengan kode objek %(code)s memakai tarif PPh berbeda (%(rates)s). "
                        "Samakan pajaknya atau pisahkan ke tagihan lain.",
                        code=tax_object.code, rates=", ".join(f"{r:g}%" for r in rates[tax_object])))
            active = bill.coretax_bupot_ids.filtered(
                lambda b: b.origin == "bill" and b.state not in INACTIVE_STATES)
            for tax_object, lines in groups.items():
                dpp = bill.company_currency_id.round(sum(lines.mapped("balance")))
                tarif = rates[tax_object][0]
                date = bill.invoice_date or bill.date
                vals = {
                    "partner_id": bill.commercial_partner_id.id, "jenis": tax_object.jenis, "tarif": tarif,
                    "jumlah_bruto": dpp, "tanggal_pemotongan": date, "dok_jenis": "02",
                    "dok_nomor": (bill.ref or bill.name or "")[:100], "dok_tanggal": date,
                }
                bupot = active.filtered(lambda b: b.tax_object_id == tax_object)
                if not bupot:
                    if dpp > 0:
                        Bupot.create({**vals, "move_id": bill.id, "company_id": bill.company_id.id, "origin": "bill",
                                      "tax_object_id": tax_object.id, "fasilitas": "SKB" if tarif == 0 else "NONE"})
                elif bupot.state in EDITABLE_STATES:
                    if dpp > 0:
                        bupot.write(vals)
                    else:
                        bupot.unlink()
                        bill._coretax_note_bupot_removed(tax_object)
                # sudah di AksaTax dan DPP berubah: ditangani Task 4 (peringatan + aktivitas)
            for bupot in active.filtered(lambda b: b.state in EDITABLE_STATES and b.tax_object_id not in groups):
                tax_object = bupot.tax_object_id
                bupot.unlink()
                bill._coretax_note_bupot_removed(tax_object)

    def _coretax_note_bupot_removed(self, tax_object):
        self.message_post(body=_("Bukti potong kode objek %s dihapus karena tagihan tidak lagi memuat PPh tersebut.",
                                 tax_object.code))

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        bills = posted.filtered(lambda m: m.move_type in ("in_invoice", "in_receipt"))
        for refund in posted.filtered(lambda m: m.move_type == "in_refund"):
            origin = refund.reversed_entry_id
            if origin.state == "posted" and origin.move_type in ("in_invoice", "in_receipt"):
                bills |= origin
        bills._coretax_sync_bupot()
        return posted
