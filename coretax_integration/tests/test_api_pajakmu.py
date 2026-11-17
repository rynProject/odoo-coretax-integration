"""Kontrak Public API PajakMu terbaru: kunci grup (X-PajakMu-NPWP), alias sk_, galat baru,
event amended/reported, dan header webhook X-PajakMu-*."""
import json
import time

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from ..tools.signature import compute_signature
from .common import FAKTUR_ID, PphTestCommon, faktur_body

BUPOT_ID = "55555555-5555-4555-8555-555555555555"


@tagged("post_install", "-at_install")
class TestApiPajakMu(PphTestCommon):

    def _list_faktur(self):
        fake, patcher = self._patch({("GET", "/faktur"): (200, {"data": [], "page": 1, "limit": 1})})
        with patcher:
            self.connection.action_test_connection()
        return fake.calls[0]["headers"]

    def test_npwp_header_only_when_set(self):
        self.assertNotIn("X-PajakMu-NPWP", self._list_faktur())
        self.connection.npwp_entitas = "01.234.567.8-901.000"
        self.assertEqual(self._list_faktur()["X-PajakMu-NPWP"], "012345678901000")

    def test_npwp_entitas_length_and_retest(self):
        self.connection.state = "ok"
        self.connection.npwp_entitas = "1234567890123456"
        self.assertEqual(self.connection.state, "draft")
        with self.assertRaisesRegex(UserError, "NPWP entitas harus 15 atau 16 digit"):
            self.connection.npwp_entitas = "123"

    def test_sk_key_alias_environment(self):
        for key, env in (("sk_live_" + "x" * 32, "production"), ("sk_test_" + "x" * 32, "sandbox"),
                         ("ak_live_" + "x" * 32, "production"), ("lain", "unknown")):
            self.connection.api_key = key
            self.connection.invalidate_recordset(["environment"])
            self.assertEqual(self.connection.environment, env, key)
        self.connection.api_key = "sk_test_" + "x" * 32
        self.assertFalse(self.connection._onchange_api_key())

    def test_group_key_errors_are_readable(self):
        for code, text in (("ENTITAS_BELUM_DIPILIH", "Isi NPWP entitas di Koneksi PajakMu"),
                           ("ENTITAS_DI_LUAR_CAKUPAN", "tidak termasuk cakupan API key ini"),
                           ("INSUFFICIENT_SCOPE", "Periksa izin (scope) API key")):
            _fake, patcher = self._patch({("GET", "/faktur"): (403 if code != "ENTITAS_BELUM_DIPILIH" else 400,
                                                               {"error": {"code": code, "message": "x"}})})
            with patcher:
                self.connection.action_test_connection()
            self.assertIn(text, self.connection.last_error, code)

    def _approved_faktur(self):
        invoice = self._invoice()
        return self.env["coretax.faktur"].create({
            "move_id": invoice.id, "state": "approved", "aksatax_id": FAKTUR_ID, "nomor_faktur": "04002600000000001"})

    def _event(self, event_type, object_id, **data):
        return {"id": "evt_1", "sequence": 1, "type": event_type, "object": {"type": "x", "id": object_id},
                "data": {"id": object_id, **data}}

    def test_faktur_amended_event(self):
        faktur = self._approved_faktur()
        self.connection._process_event(self._event("faktur.amended", FAKTUR_ID, status="AMENDED",
                                                   nomorPengganti="04002600000000002"))
        self.assertEqual(faktur.state, "replaced")
        self.assertIn("diganti oleh faktur pengganti 04002600000000002", faktur.move_id.message_ids[0].body)

    def test_faktur_amended_status_from_remote(self):
        faktur = self._approved_faktur()
        faktur._apply_remote(faktur_body("AMENDED", dilaporkan="BELUM"))
        self.assertEqual((faktur.state, faktur.lapor_state), ("replaced", "BELUM"))

    def test_faktur_reported_event(self):
        faktur = self._approved_faktur()
        self.connection._process_event(self._event("faktur.reported", FAKTUR_ID, status="APPROVED", dilaporkan="YA"))
        self.assertEqual((faktur.state, faktur.lapor_state), ("approved", "YA"))

    def test_bupot_amended_event(self):
        bill = self._bill([(1_000_000, self.pph23, self.obj_teknik)])
        bupot = bill.coretax_bupot_ids
        bupot.write({"aksatax_id": BUPOT_ID, "state": "approved"})
        self.connection._process_event(self._event("bupot.amended", BUPOT_ID, status="AMENDED"))
        self.assertEqual(bupot.state, "replaced")

    def test_new_events_followed(self):
        fake, patcher = self._patch({("GET", "/events"): (200, {"data": [], "next_after": 0, "has_more": False})})
        with patcher:
            self.connection._sync()
        types = fake.calls[0]["params"]["type"].split(",")
        for event_type in ("faktur.amended", "faktur.reported", "bupot.amended"):
            self.assertIn(event_type, types)


@tagged("post_install", "-at_install")
class TestWebhookHeaders(HttpCase):

    def setUp(self):
        super().setUp()
        company = self.env.company
        self.connection = self.env["coretax.connection"]._for_company(company, raise_if_missing=False) or \
            self.env["coretax.connection"].create({"company_id": company.id})
        self.connection.webhook_secret = "whsec_uji"

    def _post(self, headers):
        body = json.dumps({"id": "evt_9", "type": "faktur.approved", "object": {"type": "faktur", "id": "lain"},
                           "data": {"id": "lain", "status": "APPROVED"}}).encode()
        ts = str(int(time.time()))
        signature = f"t={ts},v1={compute_signature('whsec_uji', ts, body)}"
        return self.url_open(f"/coretax/webhook/{self.connection.id}", data=body,
                             headers={"Content-Type": "application/json",
                                      **{name: signature if value == "SIG" else value for name, value in headers.items()}})

    def test_pajakmu_headers_accepted(self):
        response = self._post({"X-PajakMu-Signature": "SIG", "X-PajakMu-Delivery": "dlv_baru"})
        self.assertEqual(response.status_code, 200)
        log = self.env["coretax.log"].search([("connection_id", "=", self.connection.id)], order="id desc", limit=1)
        self.assertEqual(log.request_id, "dlv_baru")

    def test_legacy_headers_still_accepted(self):
        response = self._post({"AksaTax-Signature": "SIG", "AksaTax-Delivery": "dlv_lama"})
        self.assertEqual(response.status_code, 200)

    def test_bad_signature_rejected(self):
        response = self._post({"X-PajakMu-Signature": "t=1,v1=00"})
        self.assertEqual(response.status_code, 401)
