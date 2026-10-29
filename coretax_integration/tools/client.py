"""Klien Public API AksaTax (https://<host>/api/v1/public).

Murni HTTP - tidak tahu apa pun tentang model Odoo, supaya mudah diuji.
Kontrak (aksatax-be/apps/api/src/integrasi/public-api.controller.ts):

  * Autentikasi  : header ``Authorization: Bearer ak_live_…`` / ``ak_test_…``
                   (satu API key = satu wajib pajak/NPWP di AksaTax).
  * POST         : wajib ``Idempotency-Key`` (≤255 karakter); kunci yang sama
                   + isi yang sama = jawaban yang sama diputar ulang 24 jam.
  * Galat        : ``{"error": {"code", "message", "details"?}}``.
  * Batas laju   : 120 permintaan/menit per key (429 + ``Retry-After``).
"""
import json
import logging
import re
import time

import requests

_logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/public"
TIMEOUT = 25
# Dicoba ulang sekali bila AksaTax sibuk/batas laju (aman: POST ber-Idempotency-Key).
RETRY_STATUSES = {429, 502, 503, 504}
RETRY_MAX_WAIT = 5
# Pesan NotFoundException NestJS untuk rute yang tidak ada ("Cannot POST /api/v1/public/…").
ROUTE_NOT_FOUND = re.compile(r"^Cannot (GET|POST|PUT|PATCH|DELETE) ")

# Pesan galat untuk pengguna non-teknis, menurut kode galat AksaTax.
FRIENDLY_ERRORS = {
    "UNAUTHORIZED": "API key AksaTax salah, sudah dicabut, atau kedaluwarsa. Minta API key baru ke admin AksaTax.",
    "API_KEY_ORPHANED": "Pembuat API key ini sudah tidak terdaftar di perusahaan pada AksaTax. Buat API key baru.",
    "FORBIDDEN": "API key ini tidak punya izin untuk tindakan tersebut. Periksa scope API key di AksaTax.",
    "PLAN_FEATURE": "Paket langganan AksaTax Anda belum mencakup Public API (minimal paket Business).",
    "QUOTA_EXCEEDED": "Kuota dokumen bulanan di AksaTax sudah habis. Hubungi admin AksaTax untuk menambah kuota.",
    "TENANT_FROZEN": "Akun perusahaan di AksaTax sedang dibekukan (misalnya tagihan belum dibayar).",
    "RATE_LIMITED": "Terlalu banyak permintaan dalam satu menit. Tunggu sebentar lalu coba lagi.",
    "VALIDATION": "Data ditolak AksaTax karena belum lengkap atau tidak sesuai aturan.",
    "IDEMPOTENCY_CONFLICT": "Dokumen ini pernah dikirim dengan isi berbeda. Buat ulang e-Faktur dari Odoo.",
    "IDEMPOTENCY_IN_PROGRESS": "Kiriman sebelumnya masih diproses AksaTax. Coba lagi beberapa detik lagi.",
    "NOT_FOUND": "Dokumen tidak ditemukan di AksaTax (mungkin sudah dihapus di sana).",
    "NOT_SUPPORTED": "Fitur ini butuh AksaTax versi terbaru. Minta admin AksaTax memperbarui server.",
    "UNAVAILABLE": "Layanan AksaTax sedang tidak tersedia. Coba lagi nanti.",
}
STATUS_ERRORS = {
    401: "UNAUTHORIZED",
    402: "QUOTA_EXCEEDED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    423: "TENANT_FROZEN",
    429: "RATE_LIMITED",
}


class AksaTaxError(Exception):
    """Galat dari AksaTax atau jaringan, dengan pesan yang bisa dibaca
    pengguna (`user_message`) dan rincian teknis untuk log."""

    def __init__(self, user_message, code=None, status=None, details=None, request_id=None, technical=None, data=None):
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code
        self.status = status
        self.details = details or []
        self.request_id = request_id
        self.technical = technical or ""
        # `details` berbentuk objek (bukan daftar galat per field), mis. {"id": …} pada COUNTERPARTY_EXISTS
        self.data = data or {}

    def full_message(self):
        lines = [self.user_message]
        for detail in self.details:
            path = detail.get("path") if isinstance(detail, dict) else ""
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            if isinstance(path, list):
                path = ".".join(str(p) for p in path)
            lines.append(f"• {path + ': ' if path else ''}{message}")
        return "\n".join(lines)


class AksaTaxClient:
    """Satu klien per koneksi. `logger` (opsional) dipanggil setelah setiap
    permintaan dengan dict ringkas untuk Riwayat Sinkronisasi."""

    def __init__(self, base_url, api_key, logger=None, session=None, timeout=TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.logger = logger
        self.session = session or requests.Session()
        self.timeout = timeout

    # ── inti ──────────────────────────────────────────────────────────
    def request(self, method, path, params=None, payload=None, idempotency_key=None, operation="", raw=False):
        if not self.base_url or not self.api_key:
            raise AksaTaxError("Koneksi AksaTax belum diatur. Isi alamat dan API key di Coretax › Pengaturan › Koneksi AksaTax.",
                               code="NOT_CONFIGURED")
        url = f"{self.base_url}{API_PREFIX}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/pdf, application/json" if raw else "application/json",
                   "User-Agent": "Odoo-Coretax-Integration/1.0"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if method == "POST":
            if not idempotency_key:
                raise ValueError("POST ke AksaTax wajib memakai Idempotency-Key")
            headers["Idempotency-Key"] = idempotency_key[:255]
        started = time.monotonic()
        response = None
        try:
            response = self._send(method, url, params, payload, headers)
        except requests.Timeout as exc:
            error = AksaTaxError("AksaTax tidak menjawab tepat waktu. Periksa koneksi internet server lalu coba lagi.",
                                 code="TIMEOUT", technical=str(exc))
            self._log(method, path, operation, None, started, error=error, payload=payload)
            raise error from exc
        except requests.RequestException as exc:
            error = AksaTaxError("Tidak bisa terhubung ke AksaTax. Periksa alamat AksaTax dan koneksi internet server.",
                                 code="CONNECTION", technical=str(exc))
            self._log(method, path, operation, None, started, error=error, payload=payload)
            raise error from exc

        request_id = response.headers.get("X-Request-Id")
        if raw and response.status_code < 400:
            content = response.content or b""
            self._log(method, path, operation, response, started, payload=payload,
                      body={"binary": True, "bytes": len(content)})
            return content
        body = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        if response.status_code >= 400 or body is None and response.status_code != 204:
            error = self._error_from_response(response, body, request_id)
            self._log(method, path, operation, response, started, error=error, payload=payload, body=body)
            raise error
        self._log(method, path, operation, response, started, payload=payload, body=body)
        return body if body is not None else {}

    def _send(self, method, url, params, payload, headers):
        data = json.dumps(payload) if payload is not None else None
        response = self.session.request(method, url, params=params, data=data, headers=headers, timeout=self.timeout)
        if response.status_code in RETRY_STATUSES:
            try:
                wait = min(float(response.headers.get("Retry-After") or 1), RETRY_MAX_WAIT)
            except ValueError:
                wait = 1
            time.sleep(wait)
            response = self.session.request(method, url, params=params, data=data, headers=headers, timeout=self.timeout)
        return response

    def _error_from_response(self, response, body, request_id):
        err = body.get("error") if isinstance(body, dict) else None
        if not isinstance(err, dict):  # NestJS 404 bawaan: "error" berupa string
            err = None
        code = (err or {}).get("code") or STATUS_ERRORS.get(response.status_code) or (
            "UNAVAILABLE" if response.status_code >= 500 else "HTTP_%s" % response.status_code)
        if response.status_code == 404 and (not err or (
                err.get("code") == "NOT_FOUND" and ROUTE_NOT_FOUND.match(err.get("message") or ""))):
            # Rute belum ada di AksaTax versi lama: amplop NestJS bawaan, atau amplop AksaTax
            # {"error": {"code": "NOT_FOUND", "message": "Cannot POST …"}}. 404 domain tetap NOT_FOUND.
            code = "NOT_SUPPORTED"
        if response.status_code == 403 and err and "paket" in (err.get("message") or "").lower():
            code = "PLAN_FEATURE"
        friendly = FRIENDLY_ERRORS.get(code)
        server_message = (err or {}).get("message") or ""
        if code == "VALIDATION" and server_message:
            friendly = f"{friendly} {server_message}"
        elif not friendly:
            friendly = server_message or f"AksaTax mengembalikan galat HTTP {response.status_code}."
        if code == "RATE_LIMITED" and response.headers.get("Retry-After"):
            friendly += f" (coba lagi dalam {response.headers['Retry-After']} detik)"
        raw_details = (err or {}).get("details")
        return AksaTaxError(friendly, code=code, status=response.status_code,
                            details=raw_details if isinstance(raw_details, list) else [], request_id=request_id,
                            technical=(response.text or "")[:2000],
                            data=raw_details if isinstance(raw_details, dict) else {})

    def _log(self, method, path, operation, response, started, error=None, payload=None, body=None):
        if not self.logger:
            return
        try:
            self.logger({
                "operation": operation or f"{method} {path}",
                "method": method,
                "path": f"{API_PREFIX}{path}",
                "status_code": response.status_code if response is not None else 0,
                "success": error is None,
                "request_id": (response.headers.get("X-Request-Id") if response is not None else "") or "",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "message": error.full_message() if error else "",
                "technical": (error.technical if error else "")[:4000],
                "payload": json.dumps(payload, ensure_ascii=False)[:8000] if payload is not None else "",
                "response": json.dumps(body, ensure_ascii=False, default=str)[:8000] if body is not None else "",
            })
        except Exception:  # noqa: BLE001 - log sinkronisasi tidak boleh menggagalkan proses utama
            _logger.exception("Gagal mencatat riwayat AksaTax")

    # ── endpoint ──────────────────────────────────────────────────────
    def list_faktur(self, limit=1, **filters):
        return self.request("GET", "/faktur", params={"limit": limit, **filters}, operation="Daftar faktur")

    def create_faktur(self, payload, idempotency_key):
        return self.request("POST", "/faktur", payload=payload, idempotency_key=idempotency_key,
                            operation="Buat e-Faktur (draft)")

    def get_faktur(self, faktur_id):
        return self.request("GET", f"/faktur/{faktur_id}", operation="Ambil e-Faktur")

    def send_faktur(self, faktur_id, idempotency_key):
        return self.request("POST", f"/faktur/{faktur_id}/kirim", payload={}, idempotency_key=idempotency_key,
                            operation="Kirim e-Faktur ke DJP")

    def document_status(self, jenis, document_id):
        return self.request("GET", f"/dokumen/{jenis}/{document_id}", operation="Status dokumen")

    def events(self, after=0, types=None, limit=200):
        params = {"after": after, "limit": limit}
        if types:
            params["type"] = ",".join(types)
        return self.request("GET", "/events", params=params, operation="Ambil event")

    def find_counterparties(self, npwp=None, query=None):
        params = {}
        if npwp:
            params["npwp"] = npwp
        if query:
            params["q"] = query
        return self.request("GET", "/lawan-transaksi", params=params, operation="Cari lawan transaksi").get("data", [])

    def create_counterparty(self, payload, idempotency_key):
        """NPWP yang sudah terdaftar → AksaTaxError COUNTERPARTY_EXISTS, `data["id"]` = lawan transaksi yang ada."""
        return self.request("POST", "/lawan-transaksi", payload=payload, idempotency_key=idempotency_key,
                            operation="Daftarkan lawan transaksi")

    def create_bupot_unifikasi(self, payload, idempotency_key):
        return self.request("POST", "/bupot-unifikasi", payload=payload, idempotency_key=idempotency_key,
                            operation="Buat bukti potong (draft)")

    def update_counterparty(self, counterparty_id, payload):
        return self.request("PUT", f"/lawan-transaksi/{counterparty_id}", payload=payload,
                            operation="Perbarui lawan transaksi")

    def cancel_faktur(self, faktur_id, alasan, idempotency_key):
        return self.request("POST", f"/faktur/{faktur_id}/batal", payload={"alasan": alasan},
                            idempotency_key=idempotency_key, operation="Batalkan e-Faktur")

    def faktur_pdf(self, faktur_id):
        return self.request("GET", f"/faktur/{faktur_id}/pdf", operation="Unduh PDF e-Faktur", raw=True)

    def create_retur(self, payload, idempotency_key):
        return self.request("POST", "/nota-retur", payload=payload, idempotency_key=idempotency_key,
                            operation="Buat nota retur (draft)")

    def get_retur(self, retur_id):
        return self.request("GET", f"/nota-retur/{retur_id}", operation="Ambil nota retur")

    def send_retur(self, retur_id, idempotency_key):
        return self.request("POST", f"/nota-retur/{retur_id}/kirim", payload={}, idempotency_key=idempotency_key,
                            operation="Kirim nota retur ke DJP")

    def cancel_retur(self, retur_id, alasan, idempotency_key):
        return self.request("POST", f"/nota-retur/{retur_id}/batal", payload={"alasan": alasan},
                            idempotency_key=idempotency_key, operation="Batalkan nota retur")
