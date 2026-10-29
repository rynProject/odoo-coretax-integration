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
        self.assertTrue(bill.activity_ids.filtered(lambda a: sent.name in (a.summary or "")))
