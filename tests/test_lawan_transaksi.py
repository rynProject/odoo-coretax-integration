from odoo.tests import tagged

from .common import CP_ID, FAKTUR_ID, CoretaxTestCommon, faktur_body

CP_BARU = "33333333-3333-4333-8333-333333333333"


@tagged("post_install", "-at_install", "post_install_l10n")
class TestLawanTransaksi(CoretaxTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner_a.write({"name": "PT Pelanggan", "street": "Jl. Merdeka 1", "city": "Jakarta", "zip": "10110",
                             "email": "pajak@pelanggan.test"})

    def _created(self, params, body):
        return 201, {"id": CP_BARU, "jenisId": body["jenisId"], "npwp": body["npwp"], "nama": body["nama"],
                     "vswpStatus": None}

    # ── data yang dikirim ─────────────────────────────────────────────
    def test_payload_uses_default_partner_fields(self):
        payload = self.partner_a._coretax_counterparty_payload(self.connection)
        self.assertEqual(payload, {
            "jenisId": "NPWP", "npwp": "1234567890123457", "nik": "", "nomorIdLain": "", "nitku": "",
            "nama": "PT Pelanggan", "alamat": "Jl. Merdeka 1, Jakarta, 10110",
            "email": "pajak@pelanggan.test", "negara": "IDN",
        })

    def test_payload_follows_custom_field_mapping(self):
        # mis. NPWP disimpan di field lain (field kustom/Studio) - di sini memakai `ref`
        self.connection.map_npwp_field_id = self.env["ir.model.fields"]._get("res.partner", "ref")
        self.partner_a.ref = "0013456785073000"
        payload = self.partner_a._coretax_counterparty_payload(self.connection)
        self.assertEqual(payload["npwp"], "0013456785073000")
        self.assertEqual(self.partner_a._coretax_identity(), "0013456785073000",
                         "pencocokan dan pemeriksaan memakai pemetaan yang sama")

    def test_payload_foreign_buyer_uses_passport_and_iso3_country(self):
        buyer = self.env["res.partner"].create({
            "name": "Tan Ah Kow", "street": "1 Raffles Place", "city": "Singapore", "is_company": False,
            "country_id": self.env.ref("base.sg").id, "l10n_id_buyer_document_type": "Passport",
            "l10n_id_buyer_document_number": "E1234567",
        })
        payload = buyer._coretax_counterparty_payload(self.connection)
        self.assertEqual((payload["jenisId"], payload["nomorIdLain"], payload["npwp"], payload["negara"]),
                         ("PASPOR", "E1234567", "", "SGP"))

    def test_register_check_lists_missing_data(self):
        partner = self.env["res.partner"].create({"name": "CV Tanpa Data", "is_company": True,
                                                  "country_id": self.env.ref("base.id").id})
        problems = partner._coretax_register_check(self.connection)
        self.assertTrue(any("NPWP" in p for p in problems))
        self.assertTrue(any("Alamat" in p for p in problems))
        self.assertFalse(self.partner_a._coretax_register_check(self.connection))

    # ── mendaftarkan ──────────────────────────────────────────────────
    def test_register_creates_counterparty_and_links_partner(self):
        fake, patcher = self._patch({("POST", "/lawan-transaksi"): self._created})
        with patcher:
            result = self.partner_a._coretax_register(self.connection)
        self.assertEqual(result, "created")
        self.assertEqual(self.partner_a.coretax_counterparty_id, CP_BARU)
        self.assertEqual(self.partner_a.coretax_counterparty_name, "PT Pelanggan")
        [call] = fake.calls
        self.assertTrue(call["headers"].get("Idempotency-Key"))
        self.assertEqual(call["json"]["npwp"], "1234567890123457")

    def test_register_existing_npwp_links_to_existing_counterparty(self):
        _fake, patcher = self._patch({
            ("POST", "/lawan-transaksi"): (409, {"error": {"code": "COUNTERPARTY_EXISTS",
                                                           "message": "NPWP ini sudah terdaftar sebagai PT LAMA",
                                                           "details": {"id": CP_ID}}}),
            ("GET", "/lawan-transaksi"): (200, {"data": [{"id": CP_ID, "jenisId": "NPWP", "npwp": "1234567890123457",
                                                         "nama": "PT LAMA", "vswpStatus": "AKTIF"}]}),
        })
        with patcher:
            result = self.partner_a._coretax_register(self.connection)
        self.assertEqual(result, "linked")
        self.assertEqual(self.partner_a.coretax_counterparty_id, CP_ID)
        self.assertEqual(self.partner_a.coretax_vswp_status, "AKTIF")

    def test_register_wizard_registers_ready_partners_and_skips_incomplete(self):
        incomplete = self.env["res.partner"].create({"name": "CV Tanpa Data", "is_company": True,
                                                     "country_id": self.env.ref("base.id").id})
        wizard = self.env["coretax.partner.register.wizard"].with_context(
            default_partner_ids=[(6, 0, (self.partner_a | incomplete).ids)]).create({})
        levels = {line.partner_id: line.level for line in wizard.line_ids}
        self.assertEqual(levels, {self.partner_a: "ok", incomplete: "error"})
        fake, patcher = self._patch({("POST", "/lawan-transaksi"): self._created})
        with patcher:
            wizard.action_register()
        results = {line.partner_id: line.result for line in wizard.line_ids}
        self.assertEqual(results, {self.partner_a: "created", incomplete: "skipped"})
        self.assertEqual(len(fake.calls), 1, "kontak yang datanya belum lengkap tidak dikirim")

    # ── otomatis saat mengirim e-Faktur ───────────────────────────────
    def test_send_registers_unknown_customer_when_enabled(self):
        self.assertTrue(self.connection.auto_register_partner, "aktif secara bawaan")
        move = self._invoice()
        _fake, patcher = self._patch({
            ("GET", "/lawan-transaksi"): (200, {"data": []}),
            ("POST", "/lawan-transaksi"): self._created,
            ("POST", "/faktur"): (201, faktur_body()),
            ("POST", f"/faktur/{FAKTUR_ID}/kirim"): (202, {"id": FAKTUR_ID, "status": "SUBMITTED"}),
        })
        with patcher:
            faktur = move._coretax_create_faktur(mode="send", connection=self.connection)
        self.assertEqual(faktur.state, "submitted")
        self.assertEqual(self.partner_a.with_company(move.company_id).coretax_counterparty_id, CP_BARU)
