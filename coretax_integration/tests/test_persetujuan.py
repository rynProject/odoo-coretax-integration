"""Persetujuan internal PajakMu (berjenjang): POST /faktur/{id}/kirim menjawab 202 PENDING_APPROVAL,
PajakMu lama menjawab 409; penyetuju menolak / pengajuan ditarik (DRAFT), atau disetujui tanpa terkirim
(VALIDATED); event faktur.approval_rejected / faktur.approval_withdrawn."""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import CP_ID, FAKTUR_ID, CoretaxTestCommon, faktur_body

KIRIM = ("POST", f"/faktur/{FAKTUR_ID}/kirim")
GET = ("GET", f"/faktur/{FAKTUR_ID}")
NO_EVENTS = {("GET", "/events"): (200, {"data": [], "next_after": 0, "has_more": False})}
PERSETUJUAN = {"langkah": 1, "jumlah": 2, "peran": "ADMIN_PAJAK"}
PENDING = (202, {"id": FAKTUR_ID, "status": "PENDING_APPROVAL", "attempt": None, "persetujuan": PERSETUJUAN})
# PajakMu lama: faktur yang masuk persetujuan internal ditolak kirimnya dengan 409.
CONFLICT = (409, {"error": {"code": "INVALID_STATE", "message": "Faktur menunggu persetujuan"}})


@tagged("post_install", "-at_install", "post_install_l10n")
class TestPersetujuanPajakMu(CoretaxTestCommon):

    def setUp(self):
        super().setUp()
        self.partner_a.with_company(self.company_data["company"]).coretax_counterparty_id = CP_ID

    def _send(self, kirim=PENDING, extra=None):
        move = self._invoice()
        fake, patcher = self._patch({("POST", "/faktur"): (201, faktur_body()), KIRIM: kirim, **(extra or {})})
        with patcher:
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
        return move, faktur, fake

    def _pending(self, **vals):
        move = self._invoice()
        return self.env["coretax.faktur"].create({
            "move_id": move.id, "aksatax_id": FAKTUR_ID, "state": "pending_approval", "send_attempt": 1,
            "approval_step": "Langkah 1 dari 2, menunggu Admin Pajak",
            "last_sync": fields.Datetime.now() - timedelta(hours=1), **vals})

    def _bodies(self, move):
        return " ".join(move.message_ids.mapped("body"))

    def _event(self, event_type, **data):
        return {"id": "evt_1", "sequence": 1, "type": event_type, "object": {"type": "faktur", "id": FAKTUR_ID},
                "data": {"id": FAKTUR_ID, "nomor": None, "referensi": "INV/1", "masa": 10, "tahun": 2026, **data}}

    # ── kirim: 202 PENDING_APPROVAL ───────────────────────────────────
    def test_kirim_pending_approval_not_sent(self):
        move, faktur, _fake = self._send()
        self.assertEqual(faktur.state, "pending_approval")
        self.assertFalse(faktur.submitted_at, "belum terkirim ke DJP")
        self.assertEqual(faktur.send_attempt, 1)
        self.assertEqual(faktur.approval_step, "Langkah 1 dari 2, menunggu Admin Pajak")
        self.assertFalse(faktur.last_error)
        self.assertFalse(faktur.attention)
        self.assertFalse(move.coretax_can_send, "e-Faktur yang menunggu persetujuan tidak dikirim lagi")
        body = self._bodies(move)
        self.assertIn("menunggu persetujuan di PajakMu (Langkah 1 dari 2, menunggu Admin Pajak)", body)
        self.assertNotIn("e-Faktur dibuat di PajakMu", body)

    def test_approval_step_label_without_role(self):
        faktur = self._pending()
        self.assertEqual(faktur._approval_step_label({"langkah": 2, "jumlah": 2, "peran": None}), "Langkah 2 dari 2")
        self.assertEqual(faktur._approval_step_label({"langkah": 1, "jumlah": None, "peran": "OWNER"}),
                         "Langkah 1, menunggu Owner")
        self.assertFalse(faktur._approval_step_label(None))

    def test_send_wizard_pending_not_counted_as_sent(self):
        move = self._invoice()
        wizard = self.env["coretax.send.wizard"].with_context(default_move_ids=[(6, 0, move.ids)]).create({})
        _fake, patcher = self._patch({("POST", "/faktur"): (201, faktur_body()), KIRIM: PENDING})
        with patcher:
            wizard.action_send()
        self.assertEqual(wizard.line_ids.result, "pending")
        self.assertEqual((wizard.sent_count, wizard.pending_count, wizard.failed_count), (0, 1, 0))
        self.assertIn("Langkah 1 dari 2", wizard.line_ids.result_message)
        self.assertIn("ke DJP otomatis setelah disetujui", wizard.line_ids.result_message)

    def test_send_to_djp_pending_approval(self):
        faktur = self._pending(state="draft", send_mode="draft", send_attempt=0, approval_step=False)
        _fake, patcher = self._patch({KIRIM: (202, {"id": FAKTUR_ID, "status": "PENDING_APPROVAL", "attempt": None})})
        with patcher:
            faktur.action_send_to_djp()
        self.assertEqual((faktur.state, faktur.send_mode, faktur.send_attempt), ("pending_approval", "send", 1))
        self.assertFalse(faktur.submitted_at)
        self.assertFalse(faktur.approval_step, "PajakMu tanpa `persetujuan`: tahap tidak diketahui")
        self.assertIn("e-Faktur menunggu persetujuan di PajakMu. Setelah disetujui", self._bodies(faktur.move_id))

    # ── PajakMu lama: 409, status dibaca dari GET /faktur/{id} ────────
    def test_conflict_fallback_on_create(self):
        _move, faktur, fake = self._send(kirim=CONFLICT, extra={
            GET: (200, faktur_body("PENDING_APPROVAL", persetujuan=PERSETUJUAN))})
        self.assertEqual(faktur.state, "pending_approval")
        self.assertFalse(faktur.last_error)
        self.assertFalse(faktur.submitted_at)
        self.assertEqual(faktur.approval_step, "Langkah 1 dari 2, menunggu Admin Pajak")
        self.assertEqual([c["path"] for c in fake.calls][-1], f"/faktur/{FAKTUR_ID}")

    def test_conflict_fallback_on_send_to_djp(self):
        faktur = self._pending(state="draft", send_attempt=0, approval_step=False)
        _fake, patcher = self._patch({KIRIM: CONFLICT, GET: (200, faktur_body("PENDING_APPROVAL"))})
        with patcher:
            faktur.action_send_to_djp()
        self.assertEqual((faktur.state, faktur.send_attempt), ("pending_approval", 1))
        self.assertFalse(faktur.submitted_at)

    def test_conflict_other_status_stays_error(self):
        _move, faktur, _fake = self._send(kirim=CONFLICT, extra={GET: (200, faktur_body("DRAFT"))})
        self.assertEqual(faktur.state, "draft")
        self.assertIn("Faktur menunggu persetujuan", faktur.last_error, "galat 409 tetap dilaporkan")
        self.assertTrue(faktur.attention)
        faktur = self._pending(state="draft", send_attempt=0)
        _fake, patcher = self._patch({KIRIM: CONFLICT, GET: (200, faktur_body("DRAFT"))})
        with patcher, self.assertRaises(UserError):
            faktur.action_send_to_djp()

    def test_non_conflict_error_does_not_read_status(self):
        _move, faktur, fake = self._send(kirim=(422, {"error": {"code": "VALIDATION", "message": "x"}}),
                                         extra={GET: (200, faktur_body("PENDING_APPROVAL"))})
        self.assertEqual(faktur.state, "draft")
        self.assertTrue(faktur.last_error)
        self.assertNotIn(f"/faktur/{FAKTUR_ID}", [c["path"] for c in fake.calls if c["method"] == "GET"])

    # ── polling: hasil persetujuan ────────────────────────────────────
    def _sync(self, remote):
        _fake, patcher = self._patch({GET: (200, remote), **NO_EVENTS})
        with patcher:
            return self.connection._sync()

    def test_refresh_pending_rejected_back_to_draft(self):
        faktur = self._pending()
        summary = self._sync(faktur_body("DRAFT", lastError="Dikembalikan penyetuju: NPWP pembeli salah"))
        self.assertEqual(summary["refreshed"], 1)
        self.assertEqual(faktur.state, "draft")
        self.assertEqual(faktur.last_error, "Dikembalikan penyetuju: NPWP pembeli salah")
        self.assertFalse(faktur.approval_step)
        self.assertTrue(faktur.attention, "ditolak penyetuju: perlu diperbaiki lalu dikirim lagi")
        self.assertIn("dikembalikan ke draft oleh penyetuju di PajakMu: NPWP pembeli salah.",
                      self._bodies(faktur.move_id))
        faktur.move_id.button_draft()  # tidak lagi terkunci

    def test_refresh_pending_withdrawn_back_to_draft(self):
        faktur = self._pending()
        self._sync(faktur_body("DRAFT"))
        self.assertEqual(faktur.state, "draft")
        self.assertFalse(faktur.last_error)
        self.assertFalse(faktur.attention)
        self.assertIn("Pengajuan persetujuan e-Faktur ditarik di PajakMu", self._bodies(faktur.move_id))

    def test_refresh_pending_validated_can_be_sent_again(self):
        faktur = self._pending()
        self._sync(faktur_body("VALIDATED"))
        self.assertEqual(faktur.state, "validated")
        self.assertFalse(faktur.submitted_at)
        self.assertIn("disetujui di PajakMu, tetapi belum terkirim ke DJP", self._bodies(faktur.move_id))
        _fake, patcher = self._patch({KIRIM: (202, {"id": FAKTUR_ID, "status": "SUBMITTED", "attempt": 2})})
        with patcher:
            faktur.action_send_to_djp()
        self.assertEqual((faktur.state, faktur.send_attempt), ("submitted", 2))
        self.assertTrue(faktur.submitted_at)

    def test_refresh_validated_is_polled(self):
        faktur = self._pending(state="validated", approval_step=False)
        self._sync(faktur_body("SUBMITTED"))
        self.assertEqual(faktur.state, "submitted")

    def test_refresh_still_pending_updates_step(self):
        faktur = self._pending()
        message_count = len(faktur.move_id.message_ids)
        self._sync(faktur_body("PENDING_APPROVAL", persetujuan={"langkah": 2, "jumlah": 2, "peran": "OWNER"}))
        self.assertEqual(faktur.state, "pending_approval")
        self.assertEqual(faktur.approval_step, "Langkah 2 dari 2, menunggu Owner")
        self.assertEqual(len(faktur.move_id.message_ids), message_count)

    def test_refresh_pending_submitted(self):
        faktur = self._pending()
        self._sync(faktur_body("SUBMITTED"))
        self.assertEqual(faktur.state, "submitted")
        self.assertTrue(faktur.submitted_at)
        self.assertFalse(faktur.approval_step)
        self.assertIn("e-Faktur disetujui di PajakMu dan dikirim ke DJP.", self._bodies(faktur.move_id))

    # ── event ─────────────────────────────────────────────────────────
    def test_event_submitted_after_approval(self):
        faktur = self._pending()
        self.connection._process_event(self._event("faktur.submitted", status="SUBMITTED", attempt=1))
        self.assertEqual(faktur.state, "submitted")
        self.assertTrue(faktur.submitted_at)
        self.assertIn("e-Faktur disetujui di PajakMu dan dikirim ke DJP.", self._bodies(faktur.move_id))

    def test_event_approval_rejected(self):
        faktur = self._pending()
        fake, patcher = self._patch({
            GET: (200, faktur_body("DRAFT", lastError="Dikembalikan penyetuju: Harga salah")),
            ("GET", "/events"): (200, {"data": [self._event("faktur.approval_rejected", status="DRAFT",
                                                            alasan="Harga salah")],
                                       "next_after": 1, "has_more": False}),
        })
        with patcher:
            summary = self.connection._sync()
        self.assertEqual(summary["events"], 1)
        self.assertEqual(faktur.state, "draft")
        self.assertEqual(faktur.last_error, "Dikembalikan penyetuju: Harga salah")
        self.assertIn("dikembalikan ke draft oleh penyetuju di PajakMu: Harga salah.", self._bodies(faktur.move_id))
        self.assertIn("faktur.approval_rejected", fake.calls[0]["params"]["type"].split(","))
        self.assertIn("faktur.approval_withdrawn", fake.calls[0]["params"]["type"].split(","))

    def test_event_approval_rejected_when_pajakmu_unreachable(self):
        faktur = self._pending()
        _fake, patcher = self._patch({GET: (500, {"error": {"code": "INTERNAL", "message": "x"}})})
        with patcher:
            self.connection._process_event(self._event("faktur.approval_rejected", status="DRAFT", alasan="Harga salah"))
        self.assertEqual(faktur.state, "draft")
        self.assertEqual(faktur.last_error, "Dikembalikan penyetuju: Harga salah")
        self.assertIn("oleh penyetuju di PajakMu: Harga salah.", self._bodies(faktur.move_id))

    def test_event_approval_withdrawn(self):
        faktur = self._pending()
        _fake, patcher = self._patch({GET: (200, faktur_body("DRAFT"))})
        with patcher:
            self.connection._process_event(self._event("faktur.approval_withdrawn", status="DRAFT"))
        self.assertEqual(faktur.state, "draft")
        self.assertFalse(faktur.last_error)
        self.assertIn("Pengajuan persetujuan e-Faktur ditarik di PajakMu", self._bodies(faktur.move_id))

    def test_stale_approval_event_uses_current_status(self):
        # faktur sudah diajukan lagi di PajakMu saat event penolakan lama diproses
        faktur = self._pending()
        _fake, patcher = self._patch({GET: (200, faktur_body("PENDING_APPROVAL", persetujuan=PERSETUJUAN))})
        with patcher:
            self.connection._process_event(self._event("faktur.approval_rejected", status="DRAFT", alasan="Lama"))
        self.assertEqual(faktur.state, "pending_approval")
        # PajakMu tidak bisa dibaca: event lama tidak mengembalikan e-Faktur yang sudah dikirim
        faktur.write({"state": "submitted"})
        _fake, patcher = self._patch({GET: (500, {"error": {"code": "INTERNAL", "message": "x"}})})
        with patcher:
            self.connection._process_event(self._event("faktur.approval_withdrawn", status="DRAFT"))
        self.assertEqual(faktur.state, "submitted")

    # ── kunci faktur Odoo selama menunggu persetujuan ─────────────────
    def test_reset_and_cancel_blocked_while_pending(self):
        faktur = self._pending()
        move = faktur.move_id
        for action in (move.button_draft, move.button_cancel):
            with self.assertRaises(UserError) as ctx:
                action()
            message = str(ctx.exception)
            self.assertIn("sedang menunggu persetujuan di PajakMu", message)
            self.assertIn("Tarik pengajuannya di PajakMu dulu", message)
            self.assertNotIn("Batalkan e-Faktur", message)
        self.assertEqual(move.state, "posted")
        self._sync(faktur_body("DRAFT"))  # pengajuan ditarik
        move.button_draft()
        self.assertEqual(move.state, "draft")

    def test_reset_blocked_while_replacement_pending(self):
        move = self._invoice()
        old = self.env["coretax.faktur"].create({
            "move_id": move.id, "state": "approved", "aksatax_id": "lama", "nomor_faktur": "0400260000000001"})
        old.action_request_replacement()
        move.button_draft()
        move.action_post()
        _fake, patcher = self._patch({("POST", "/faktur"): (201, faktur_body()), KIRIM: PENDING})
        with patcher:
            new = move._coretax_create_faktur(mode="send", connection=self.connection)
        self.assertEqual((new.replaces_id, new.state), (old, "pending_approval"))
        with self.assertRaisesRegex(UserError, "sedang menunggu persetujuan di PajakMu"):
            move.button_draft()
