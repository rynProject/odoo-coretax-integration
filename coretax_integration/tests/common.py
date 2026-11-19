import json
from unittest.mock import patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

FAKTUR_ID = "11111111-1111-4111-8111-111111111111"
CP_ID = "22222222-2222-4222-8222-222222222222"


class FakeResponse:
    def __init__(self, status, body=None, headers=None, raw=None):
        self.status_code = status
        self._body = body
        self.headers = {"X-Request-Id": "req-test", **(headers or {})}
        self._raw = raw
        if raw is not None:
            self.content = raw
        else:
            self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode(errors="replace")

    def json(self):
        if self._raw is not None or self._body is None:
            raise ValueError("no json")
        return self._body


class FakeAksaTax:
    """Pengganti server PajakMu: rute -> (status, isi). Mencatat setiap
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
                if callable(answer):
                    answer = answer(params, json.loads(data) if data else None)
                if isinstance(answer, FakeResponse):
                    return answer
                status, body = answer
                return FakeResponse(status, body)
        # Rute tidak dikenal: amplop PajakMu asli (AllExceptionsFilter atas NotFoundException NestJS).
        return FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": f"Cannot {method} /api/v1/public{path}"}})


def chart_ref(env, company, xmlid, raise_if_not_found=True):
    """Pengganti account.chart.template.ref (Odoo 17+): pajak/akun per perusahaan atau grup pajak global."""
    for name in (f"l10n_id.{company.id}_{xmlid}", f"coretax_integration.{company.id}_{xmlid}"):
        record = env.ref(name, raise_if_not_found=False)
        if record:
            return record
    for name in (f"l10n_id.{xmlid}", f"coretax_integration.{xmlid}"):
        record = env.ref(name, raise_if_not_found=False)
        if record and record._name == "account.tax.group":
            return record
    if raise_if_not_found:
        raise ValueError(f"xmlid chart {xmlid} tidak ditemukan")
    return None


def faktur_body(status="DRAFT", **extra):
    return {"id": FAKTUR_ID, "status": status, "nomorFaktur": None, "referensi": "INV/1", "tanggalFaktur": "2026-10-01",
            "masaPajak": 10, "tahunPajak": 2026, "kdJenisTransaksi": "04",
            "pembeli": {"counterpartyId": CP_ID, "nama": "PT Pelanggan", "npwp": "1234567890123457"},
            "totalDpp": 100000, "totalDppNilaiLain": 91667, "totalPpn": 11000, "totalPpnbm": 0,
            "kodeApproval": None, "tanggalApproval": None, "lastError": None, **extra}


class CoretaxTestCommon(AccountTestInvoicingCommon):
    """Perusahaan Indonesia, pelanggan ber-NPWP, dan koneksi PajakMu uji."""

    @classmethod
    def setUpClass(cls, chart_template_ref="l10n_id.l10n_id_chart"):
        super().setUpClass(chart_template_ref=chart_template_ref)
        company = cls.company_data["company"]
        company.write({"vat": "1234567890123456", "street": "Jl. Uji", "phone": "123"})
        cls.partner_a.write({"vat": "1234567890123457", "l10n_id_pkp": True, "l10n_id_kode_transaksi": "04",
                             "country_id": cls.env.ref("base.id").id, "is_company": True})
        cls.tax_sale_a.amount = 12.0
        cls.connection = cls.env["coretax.connection"].create({
            "company_id": company.id, "base_url": "https://aksatax.test", "api_key": "ak_test_" + "x" * 32,
        })

    @classmethod
    def _chart_ref(cls, xmlid, raise_if_not_found=True):
        return chart_ref(cls.env, cls.company_data["company"], xmlid, raise_if_not_found)

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


class PphTestCommon(CoretaxTestCommon):
    """Pajak PPh dari template l10n_id dan helper tagihan vendor ber-PPh."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pph23 = cls._chart_ref("tax_pph23_2")
        cls.pph23_skb = cls._chart_ref("tax_pph23_skb")
        cls.pph42 = cls._chart_ref("tax_pph42_sewa_10")
        cls.ppn = cls._chart_ref("tax_PT1")  # Odoo 16: PPN 11% pembelian
        cls.obj_teknik = cls.env.ref("coretax_integration.tax_object_24_104_01")
        cls.obj_konsultan = cls.env.ref("coretax_integration.tax_object_24_104_03")
        cls.obj_sewa = cls.env.ref("coretax_integration.tax_object_28_402_01")

    def _bill(self, lines, post=True, **vals):
        """lines: list (price_unit, taxes) atau (price_unit, taxes, tax_object)."""
        line_vals = []
        for line in lines:
            price, taxes = line[0], line[1]
            vals_line = {"product_id": self.product_a.id, "quantity": 1, "price_unit": price,
                         "tax_ids": [(6, 0, taxes.ids)]}
            if len(line) > 2:
                vals_line["coretax_tax_object_id"] = line[2].id
            line_vals.append((0, 0, vals_line))
        move = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.partner_a.id, "invoice_date": "2026-10-01",
            "invoice_line_ids": line_vals, **vals,
        })
        if post:
            move.action_post()
        return move
