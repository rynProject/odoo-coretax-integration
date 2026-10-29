from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .coretax_bupot import EDITABLE_STATES, INACTIVE_STATES


class AccountMove(models.Model):
    _inherit = "account.move"

    coretax_bupot_ids = fields.One2many("coretax.bupot", "move_id", string="Bukti potong PPh")
    coretax_bupot_count = fields.Integer(compute="_compute_coretax_bupot_count", compute_sudo=True, string="Jumlah bukti potong")
    coretax_has_pph = fields.Boolean(compute="_compute_coretax_has_pph", compute_sudo=True, string="Punya baris PPh")

    @api.depends("coretax_bupot_ids")
    def _compute_coretax_bupot_count(self):
        for move in self:
            move.coretax_bupot_count = len(move.coretax_bupot_ids)

    @api.depends("invoice_line_ids.tax_ids", "invoice_line_ids.coretax_tax_object_id")
    def _compute_coretax_has_pph(self):
        for move in self:
            move.coretax_has_pph = any(
                line.display_type == "product" and line.coretax_tax_object_id
                and line.tax_ids.filtered("coretax_tax_object_id")
                for line in move.invoice_line_ids)

    def action_coretax_view_bupot(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("coretax_integration.action_coretax_bupot")
        action["domain"] = [("move_id", "=", self.id)]
        action["context"] = {"active_test": False}
        if self.coretax_bupot_count == 1:
            action.update(views=[[False, "form"]], res_id=self.coretax_bupot_ids.id)
        return action

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
        # sinkronisasi memakai sudo: pengguna tanpa grup Coretax tetap harus bisa memposting/mereset tagihan
        Bupot = self.env["coretax.bupot"].sudo().with_context(coretax_bupot_sync=True)
        # state yang baru berubah (mis. ditolak) harus sudah di DB sebelum INSERT, agar unique index melihatnya
        Bupot.flush_model(["state"])
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
            active = bill.sudo().coretax_bupot_ids.filtered(
                lambda b: b.origin == "bill" and b.state not in INACTIVE_STATES)
            # bupot yang dihapus dikumpulkan dulu; unlink mengosongkan cache sehingga `active` tidak boleh dipakai lagi
            to_delete = []  # [(bupot, catatan)]
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
                        if tarif == 0 and bupot.fasilitas == "NONE":
                            vals["fasilitas"] = "SKB"
                        elif tarif > 0 and bupot.fasilitas in ("SKB", "DTP"):
                            vals.update(fasilitas="NONE", nomor_fasilitas=False)
                        bupot.write(vals)
                    else:
                        to_delete.append((bupot, _("Bukti potong kode objek %s dihapus karena DPP-nya menjadi 0 "
                                                   "setelah nota kredit.", tax_object.code)))
                elif bill.company_currency_id.compare_amounts(bupot.jumlah_bruto, dpp) != 0:
                    # sudah di AksaTax: jangan diubah, beri peringatan dan aktivitas pembetulan
                    bill._coretax_warn_sent_bupot(bupot, dpp)
            for bupot in active.filtered(lambda b: b.state in EDITABLE_STATES and b.tax_object_id not in groups):
                to_delete.append((bupot, _("Bukti potong kode objek %s dihapus karena tagihan tidak lagi memuat "
                                           "PPh tersebut.", bupot.tax_object_id.code)))
            for bupot, note in to_delete:
                bupot.with_context(coretax_bupot_sync=True).unlink()
                bill.message_post(body=note)

    def _coretax_warn_sent_bupot(self, bupot, new_dpp):
        """Catat dan jadwalkan aktivitas saat DPP bupot yang sudah di AksaTax berubah."""
        self.ensure_one()
        summary = _("Pembetulan bukti potong %(code)s (%(name)s)", code=bupot.tax_object_id.code, name=bupot.name)
        if self.activity_ids.filtered(lambda a: a.summary == summary):
            return  # peringatan untuk bupot ini sudah ada dan belum diselesaikan
        old_dpp = bupot.jumlah_bruto
        currency = self.company_currency_id
        old_text = currency.format(old_dpp)
        new_text = currency.format(new_dpp)
        bupot.message_post(body=_(
            "DPP tagihan berubah dari %(old)s menjadi %(new)s, tetapi bukti potong ini sudah ada di AksaTax "
            "dan tidak diubah otomatis.", old=old_text, new=new_text))
        user = self.invoice_user_id or self.create_uid
        if not user or user == self.env.ref("base.user_root"):
            return
        note = _("DPP tagihan %(bill)s berubah dari %(old)s menjadi %(new)s, sedangkan bukti potong %(bupot)s "
                 "sudah ada di AksaTax. Lakukan pembetulan atau pembatalan bukti potong di AksaTax.",
                 bill=self.name, old=old_text, new=new_text, bupot=bupot.name)
        self.activity_schedule("mail.mail_activity_data_todo", user_id=user.id,
                               summary=summary, note=note)

    def _coretax_bupot_guard(self, action_label):
        sent = self.filtered(lambda m: m.move_type in ("in_invoice", "in_receipt")).sudo().coretax_bupot_ids.filtered(lambda b: b.origin == "bill" and b._coretax_is_at_aksatax())
        if sent:
            raise UserError(_(
                "Tagihan %(moves)s punya bukti potong yang sudah ada di AksaTax (%(bupots)s). Batalkan dulu "
                "bukti potongnya di AksaTax sebelum %(action)s tagihan ini.",
                moves=", ".join(sent.move_id.mapped("name")), bupots=", ".join(sent.mapped("name")),
                action=action_label))

    def _coretax_refund_parents(self):
        """Tagihan induk terposting dari nota kredit/debit pembelian di `self`."""
        parents = self.filtered(lambda m: m.move_type == "in_refund").reversed_entry_id
        return parents.filtered(lambda m: m.state == "posted" and m.move_type in ("in_invoice", "in_receipt"))

    def _coretax_delete_editable_bupots(self, note=None):
        editable = self.filtered(lambda m: m.move_type in ("in_invoice", "in_receipt")).sudo().coretax_bupot_ids.filtered(lambda b: b.origin == "bill" and b.state in EDITABLE_STATES)
        moves = editable.move_id.with_env(self.env)
        editable.with_context(coretax_bupot_sync=True).unlink()
        if note:
            for move in moves:
                move.message_post(body=note)

    def button_draft(self):
        self._coretax_bupot_guard(_("mengembalikan ke draft"))
        parents = self._coretax_refund_parents()
        res = super().button_draft()
        parents._coretax_sync_bupot()
        return res

    def button_cancel(self):
        self._coretax_bupot_guard(_("membatalkan"))
        # nota kredit terposting melewati button_draft (yang sudah menyinkronkan induknya) sebelum dibatalkan
        res = super().button_cancel()
        self._coretax_delete_editable_bupots(_("Bukti potong yang belum dikirim dihapus karena tagihan dibatalkan."))
        return res

    def unlink(self):
        self._coretax_bupot_guard(_("menghapus"))
        self._coretax_delete_editable_bupots()
        return super().unlink()

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        bills = posted.filtered(lambda m: m.move_type in ("in_invoice", "in_receipt"))
        for refund in posted.filtered(lambda m: m.move_type == "in_refund"):
            origin = refund.reversed_entry_id
            if origin.state == "posted" and origin.move_type in ("in_invoice", "in_receipt"):
                bills |= origin
        bills._coretax_sync_bupot()
        return posted
