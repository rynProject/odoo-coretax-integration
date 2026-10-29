import json
from unittest.mock import patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

FAKTUR_ID = "11111111-1111-4111-8111-111111111111"
CP_ID = "22222222-2222-4222-8222-222222222222"


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = {"X-Request-Id": "req-test", **(headers or {})}
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeAksaTax:
    """Pengganti server AksaTax: rute -> (status, isi). Mencatat setiap
    permintaan untuk diperiksa."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, session, method, url, params=None, data=None, headers=None, timeout=None):
        path = url.split("/api/v1/public", 1)[1]
        self.calls.append({"method": method, "path": path, "params": params, "headers": headers,
                           "json": json.loads(data) if data else None})
        for (route_method, route_path), answer in self.routes.items():
            if route_method == method and route_path == path:
                status, body = answer(params, json.loads(data) if data else None) if callable(answer) else answer
                return FakeResponse(status, body)
        return FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": "Tidak ditemukan"}})


def faktur_body(status="DRAFT", **extra):
    return {"id": FAKTUR_ID, "status": status, "nomorFaktur": None, "referensi": "INV/1", "tanggalFaktur": "2026-10-01",
            "masaPajak": 10, "tahunPajak": 2026, "kdJenisTransaksi": "04",
            "pembeli": {"counterpartyId": CP_ID, "nama": "PT Pelanggan", "npwp": "1234567890123457"},
            "totalDpp": 100000, "totalDppNilaiLain": 91667, "totalPpn": 11000, "totalPpnbm": 0,
            "kodeApproval": None, "tanggalApproval": None, "lastError": None, **extra}


class CoretaxTestCommon(AccountTestInvoicingCommon):
    """Perusahaan Indonesia, pelanggan ber-NPWP, dan koneksi AksaTax uji."""

    @classmethod
    @AccountTestInvoicingCommon.setup_country("id")
    def setUpClass(cls):
        super().setUpClass()
        company = cls.company_data["company"]
        company.write({"vat": "1234567890123456", "street": "Jl. Uji", "phone": "123"})
        cls.partner_a.write({"vat": "1234567890123457", "l10n_id_pkp": True, "l10n_id_kode_transaksi": "04",
                             "country_id": cls.env.ref("base.id").id, "is_company": True})
        cls.tax_sale_a.amount = 12.0
        cls.connection = cls.env["coretax.connection"].create({
            "company_id": company.id, "base_url": "https://aksatax.test", "api_key": "ak_test_" + "x" * 32,
        })

    def _invoice(self, **vals):
        move = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner_a.id, "invoice_date": "2026-10-01",
            "invoice_line_ids": [(0, 0, {"product_id": self.product_a.id, "quantity": 2, "price_unit": 50000,
                                         "discount": 10, "tax_ids": [(6, 0, self.tax_sale_a.ids)]})],
            **vals,
        })
        move.action_post()
        return move

    def _patch(self, routes):
        fake = FakeAksaTax(routes)
        return fake, patch("requests.Session.request", new=lambda session, *a, **kw: fake(session, *a, **kw))
