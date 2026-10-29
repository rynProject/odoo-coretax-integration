from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import CP_ID, PphTestCommon


@tagged("post_install", "-at_install", "post_install_l10n")
class TestPphBupot(PphTestCommon):

    def _three_lines(self, **kw):
        return self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23), (20_000_000, self.pph42)], **kw)

    def test_case_three_lines_creates_two_bupots(self):
        bill = self._three_lines()
        bupots = bill.coretax_bupot_ids.sorted(lambda b: b.tax_object_id.code)
        self.assertRecordValues(bupots, [
            {"origin": "bill", "state": "new", "tax_object_id": self.obj_teknik.id, "partner_id": self.partner_a.id,
             "jenis": "BPPU", "jumlah_bruto": 15_000_000, "tarif": 2.0, "pph_gl": 300_000, "pph_estimasi": 300_000,
             "pph_selisih": 0, "fasilitas": "NONE", "dok_jenis": "02", "dok_nomor": bill.name,
             "tanggal_pemotongan": bill.invoice_date, "dok_tanggal": bill.invoice_date},
            {"origin": "bill", "state": "new", "tax_object_id": self.obj_sewa.id, "partner_id": self.partner_a.id,
             "jenis": "BPPU", "jumlah_bruto": 20_000_000, "tarif": 10.0, "pph_gl": 2_000_000, "pph_estimasi": 2_000_000,
             "pph_selisih": 0, "fasilitas": "NONE", "dok_jenis": "02", "dok_nomor": bill.name,
             "tanggal_pemotongan": bill.invoice_date, "dok_tanggal": bill.invoice_date},
        ])
        self.assertEqual(bupots[0].source_line_ids, bill.invoice_line_ids[:2])
        self.assertEqual(bupots[1].source_line_ids, bill.invoice_line_ids[2])
        self.assertEqual(bupots[0].tax_line_ids.tax_line_id, self.pph23)

    def test_changed_object_creates_three_bupots(self):
        bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23, self.obj_konsultan),
                           (20_000_000, self.pph42)])
        self.assertEqual(sorted(zip(bill.coretax_bupot_ids.mapped("tax_object_id.code"),
                                    bill.coretax_bupot_ids.mapped("pph_gl"))),
                         [("24-104-01", 200_000), ("24-104-03", 100_000), ("28-402-01", 2_000_000)])

    def test_skb_bupot_without_journal_item(self):
        bill = self._bill([(10_000_000, self.pph23_skb)])
        self.assertRecordValues(bill.coretax_bupot_ids, [
            {"tax_object_id": self.obj_teknik.id, "jumlah_bruto": 10_000_000, "tarif": 0.0, "pph_gl": 0, "fasilitas": "SKB"}])
        self.assertFalse(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23_skb))

    def test_mixed_rates_same_object_rejected(self):
        bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23_skb)], post=False)
        with self.assertRaisesRegex(UserError, "24-104-01"):
            bill.action_post()

    def test_foreign_currency_bill_dpp_in_idr(self):
        usd = self.setup_other_currency("USD", rates=[("2026-01-01", 0.0001)])  # 1 USD = 10.000 IDR
        bill = self._bill([(1_000, self.pph23)], currency_id=usd.id)
        self.assertEqual(bill.coretax_bupot_ids.jumlah_bruto, 10_000_000)
        self.assertEqual(bill.coretax_bupot_ids.pph_gl, 200_000)

    def test_analytic_split_still_one_bupot(self):
        plan = self.env["account.analytic.plan"].create({"name": "Proyek"})
        a1, a2 = self.env["account.analytic.account"].create([{"name": n, "plan_id": plan.id} for n in ("A1", "A2")])
        bill = self._bill([(10_000_000, self.pph23), (5_000_000, self.pph23)], post=False)
        bill.invoice_line_ids[0].analytic_distribution = {str(a1.id): 100}
        bill.invoice_line_ids[1].analytic_distribution = {str(a2.id): 100}
        bill.action_post()
        self.assertEqual(len(bill.line_ids.filtered(lambda l: l.tax_line_id == self.pph23)), 2)
        self.assertRecordValues(bill.coretax_bupot_ids, [{"jumlah_bruto": 15_000_000, "pph_gl": 300_000}])

    def test_auto_bupot_send_payload_and_guards(self):
        bill = self._three_lines()
        bupot = bill.coretax_bupot_ids.filtered(lambda b: b.tax_object_id == self.obj_teknik)
        with self.assertRaises(UserError):
            bupot.unlink()
        self.partner_a.with_company(bill.company_id).coretax_counterparty_id = CP_ID
        fake, patcher = self._patch({
            ("POST", "/bupot-unifikasi"): (201, {"id": "33333333-3333-4333-8333-333333333333", "status": "DRAFT"})})
        with patcher:
            bupot.action_send_draft()
        sent = fake.calls[-1]["json"]
        self.assertEqual((sent["kodeObjek"], sent["jumlahBruto"]), ("24-104-01", 15_000_000))

    def test_send_blocked_while_bill_draft(self):
        bill = self._three_lines()
        bill.button_draft()
        with self.assertRaisesRegex(UserError, "belum diposting"):
            bill.coretax_bupot_ids[0].action_send_draft()

    def test_full_reversal_deletes_new_bupots(self):
        bill = self._three_lines()
        self.assertEqual(len(bill.coretax_bupot_ids), 2)
        bill._reverse_moves([{"invoice_date": bill.invoice_date}], cancel=True)
        self.assertFalse(bill.coretax_bupot_ids)
        self.assertIn("DPP-nya menjadi 0", bill.message_ids[0].body)

    def test_repost_removes_stale_group(self):
        bill = self._three_lines()
        bill.button_draft()
        bill.invoice_line_ids[2].tax_ids = [(5, 0, 0)]
        bill.action_post()
        self.assertEqual(bill.coretax_bupot_ids.tax_object_id, self.obj_teknik)
        self.assertIn("tidak lagi memuat", bill.message_ids[0].body)

    def test_repost_keeps_facility(self):
        bill = self._three_lines()
        bupot = bill.coretax_bupot_ids.filtered(lambda b: b.tax_object_id == self.obj_teknik)
        bupot.write({"fasilitas": "SKD", "nomor_fasilitas": "SKD-1"})
        bill.button_draft()
        bill.action_post()
        self.assertRecordValues(bupot, [{"fasilitas": "SKD", "nomor_fasilitas": "SKD-1"}])

    def _teknik(self, bill):
        return bill.coretax_bupot_ids.filtered(lambda b: b.tax_object_id == self.obj_teknik and b.state != "rejected")

    def test_repost_updates_same_bupot(self):
        bill = self._three_lines()
        before = self._teknik(bill)
        bill.button_draft()
        bill.invoice_line_ids[0].price_unit = 20_000_000
        bill.action_post()
        self.assertEqual(self._teknik(bill), before)
        self.assertEqual(before.jumlah_bruto, 25_000_000)

    def test_reset_and_cancel_blocked_when_bupot_at_aksatax(self):
        bill = self._three_lines()
        self._teknik(bill).write({"aksatax_id": "x-1", "state": "draft"})
        with self.assertRaisesRegex(UserError, "AksaTax"):
            bill.button_draft()
        with self.assertRaisesRegex(UserError, "AksaTax"):
            bill.button_cancel()

    def test_cancel_and_unlink_delete_new_bupots(self):
        bill = self._three_lines()
        bill.button_draft()
        bill.button_cancel()
        self.assertFalse(bill.coretax_bupot_ids)
        other = self._three_lines()
        other.button_draft()
        bupot_ids = other.coretax_bupot_ids.ids
        other.unlink()
        self.assertFalse(self.env["coretax.bupot"].browse(bupot_ids).exists())

    def test_repost_after_rejected_creates_new_bupot(self):
        bill = self._three_lines()
        old = self._teknik(bill)
        old.write({"aksatax_id": "x-1", "state": "rejected"})
        bill.button_draft()
        bill.action_post()
        new = self._teknik(bill)
        self.assertTrue(new and new != old)
        self.assertEqual(old.state, "rejected")

    def test_partial_credit_note_nets_dpp_and_reset_restores(self):
        bill = self._three_lines()
        refund = bill._reverse_moves([{"invoice_date": bill.invoice_date}])
        refund.invoice_line_ids[1:].unlink()
        refund.invoice_line_ids.price_unit = 4_000_000
        refund.action_post()
        self.assertRecordValues(self._teknik(bill), [{"jumlah_bruto": 11_000_000, "pph_gl": 220_000}])
        self.assertEqual(bill.coretax_bupot_ids.filtered(lambda b: b.tax_object_id == self.obj_sewa).jumlah_bruto, 20_000_000)
        refund.button_draft()
        self.assertEqual(self._teknik(bill).jumlah_bruto, 15_000_000)

    def test_credit_note_on_sent_bupot_schedules_activity(self):
        bill = self._three_lines()
        sent = self._teknik(bill)
        sent.write({"aksatax_id": "x-1", "state": "approved"})
        bill._reverse_moves([{"invoice_date": bill.invoice_date}], cancel=True)
        self.assertEqual(sent.jumlah_bruto, 15_000_000)
        self.assertTrue(bill.activity_ids.filtered(lambda a: sent.tax_object_id.code in (a.summary or "")))

    def _partial_refund(self, bill):
        refund = bill._reverse_moves([{"invoice_date": bill.invoice_date}])
        refund.invoice_line_ids[1:].unlink()
        refund.invoice_line_ids.price_unit = 4_000_000
        refund.action_post()
        return refund

    def test_cancel_posted_credit_note_restores_dpp(self):
        bill = self._three_lines()
        refund = self._partial_refund(bill)
        self.assertEqual(self._teknik(bill).jumlah_bruto, 11_000_000)
        refund.button_cancel()
        self.assertEqual(self._teknik(bill).jumlah_bruto, 15_000_000)

    def test_cancel_credit_note_on_sent_bupot_single_activity(self):
        bill = self._three_lines()
        sent = self._teknik(bill)
        sent.write({"aksatax_id": "x-1", "state": "approved"})
        refund = self._partial_refund(bill)
        refund.button_cancel()
        summary = "Pembetulan bukti potong %s (%s)" % (sent.tax_object_id.code, sent.name)
        self.assertEqual(len(bill.activity_ids.filtered(lambda a: a.summary == summary)), 1)
        bill._coretax_sync_bupot()
        self.assertEqual(len(bill.activity_ids.filtered(lambda a: a.summary == summary)), 1)

    def test_orphan_rejected_bupot_can_be_unlinked(self):
        bill = self._three_lines()
        bupot = self._teknik(bill)
        bupot.write({"aksatax_id": "x-1", "state": "rejected"})
        bupot.move_id = False
        bupot.unlink()
        self.assertFalse(bupot.exists())

    def test_cancel_posted_credit_note_syncs_parent_once(self):
        bill = self._three_lines()
        refund = self._partial_refund(bill)
        original = type(bill)._coretax_sync_bupot
        calls = []

        def counting(records):
            calls.append(records.ids)
            return original(records)

        with patch.object(type(bill), "_coretax_sync_bupot", autospec=True, side_effect=counting):
            refund.button_cancel()
        self.assertEqual(calls, [bill.ids])

    # --- temuan review akhir ---

    def _plain_user(self, *xmlids):
        company = self.company_data["company"]
        groups = self.env.ref("base.group_user")
        for xmlid in xmlids:
            groups |= self.env.ref(xmlid)
        return self.env["res.users"].create({
            "name": "Pengguna biasa", "login": "plain_" + "_".join(xmlids or ["x"]),
            "company_id": company.id, "company_ids": [(6, 0, company.ids)], "group_ids": [(6, 0, groups.ids)],
        })

    def test_readonly_user_can_read_bill_form_fields(self):
        bill = self._three_lines()
        user = self._plain_user("account.group_account_readonly")
        self.assertFalse(user.has_group("coretax_integration.group_coretax_user"))
        self.env.invalidate_all()
        res = bill.with_user(user).web_read({"name": {}, "coretax_bupot_count": {}, "coretax_has_pph": {}})
        self.assertEqual(res[0]["coretax_bupot_count"], 2)
        self.assertTrue(res[0]["coretax_has_pph"])
        arch = self.env["account.move"].with_user(user).get_view(
            self.env.ref("account.view_move_form").id, "form")["arch"]
        self.assertNotIn("coretax_bupot_ids", arch)

    def test_user_without_coretax_group_can_unlink_reset_cancel_bill(self):
        # Invoicing otomatis mengimplikasikan grup Coretax; tiru pengguna Purchase/POS dengan grup
        # buatan yang punya hak penuh atas tagihan tetapi bukan pengguna Coretax.
        user = self._plain_user("account.group_account_readonly")
        group = self.env["res.groups"].create({"name": "Hak tagihan penuh (uji)"})
        for model in ("account.move", "account.move.line"):
            self.env["ir.model.access"].create({
                "name": "uji " + model, "model_id": self.env["ir.model"]._get(model).id, "group_id": group.id,
                "perm_read": True, "perm_write": True, "perm_create": True, "perm_unlink": True})
        user.write({"group_ids": [(4, group.id)]})
        self.assertFalse(user.has_group("coretax_integration.group_coretax_user"))
        draft = self._three_lines(post=False)
        self.env.invalidate_all()  # cache dibagi antar env; paksa pembacaan memeriksa hak akses
        draft.with_user(user).unlink()
        self.assertFalse(draft.exists())
        bill = self._three_lines()
        self.env.invalidate_all()
        bill.with_user(user).button_draft()
        bill.action_post()  # memposting memang butuh hak Invoicing
        self.env.invalidate_all()
        bill.with_user(user).button_draft()
        self.env.invalidate_all()
        bill.with_user(user).button_cancel()
        self.assertFalse(bill.coretax_bupot_ids)

    def test_rate_change_updates_facility_on_repost(self):
        bill = self._bill([(10_000_000, self.pph23)])
        bupot = self._teknik(bill)
        self.assertEqual(bupot.fasilitas, "NONE")
        bill.button_draft()
        bill.invoice_line_ids.tax_ids = self.pph23_skb
        bill.action_post()
        self.assertRecordValues(bupot, [{"tarif": 0.0, "fasilitas": "SKB"}])
        bupot.nomor_fasilitas = "SKB-1"
        bill.button_draft()
        bill.invoice_line_ids.tax_ids = self.pph23
        bill.action_post()
        self.assertRecordValues(bupot, [{"tarif": 2.0, "fasilitas": "NONE", "nomor_fasilitas": False}])

    def test_check_before_send_rejects_zero_rate_without_facility(self):
        bill = self._bill([(10_000_000, self.pph23)])
        bupot = self._teknik(bill)
        bupot.write({"tarif": 0.0, "fasilitas": "NONE"})
        with self.assertRaisesRegex(UserError, "tarif 0"):
            bupot._check_before_send()

    def test_two_sent_bupots_full_credit_note_two_activities(self):
        bill = self._bill([(10_000_000, self.pph23), (20_000_000, self.pph42)])
        for bupot in bill.coretax_bupot_ids:
            bupot.write({"aksatax_id": "x-%s" % bupot.id, "state": "draft"})
        bill._reverse_moves([{"invoice_date": bill.invoice_date}], cancel=True)
        self.assertEqual(len(bill.activity_ids.filtered(lambda a: "Pembetulan" in (a.summary or ""))), 2)
        for bupot in bill.coretax_bupot_ids:
            self.assertEqual(len(bupot.message_ids.filtered(lambda m: "DPP tagihan berubah" in (m.body or ""))), 1)

    def test_credit_note_after_tax_gets_object_later(self):
        tax = self.pph23.copy({"name": "PPh lama", "coretax_tax_object_id": False})
        bill = self._bill([(10_000_000, tax)], post=True)
        self.assertFalse(bill.invoice_line_ids.coretax_tax_object_id)
        tax.coretax_tax_object_id = self.obj_teknik
        refund = bill._reverse_moves([{"invoice_date": bill.invoice_date}], cancel=True)
        self.assertEqual(refund.invoice_line_ids.coretax_tax_object_id, self.obj_teknik)
