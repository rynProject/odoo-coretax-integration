import json
import logging

from odoo import http
from odoo.http import request

from ..tools.signature import verify_signature

_logger = logging.getLogger(__name__)


class CoretaxWebhookController(http.Controller):
    """Penerima webhook PajakMu: POST /coretax/webhook/<id koneksi>.

    Tanpa sesi Odoo (auth='none'); keasliannya dijamin tanda tangan HMAC
    ``X-PajakMu-Signature`` (atau header lama ``AksaTax-Signature``, nilainya
    sama) dengan Webhook Secret koneksi. Jawaban 2xx berarti diterima -
    PajakMu mengulang kiriman yang gagal (maks. 5 kali)."""

    # readonly=False: rute auth='none' di Odoo 19 bawaannya read-only, padahal webhook menulis status & riwayat.
    @http.route("/coretax/webhook/<int:connection_id>", type="http", auth="none", methods=["POST"], csrf=False,
                save_session=False, readonly=False)
    def receive(self, connection_id, **_kwargs):
        # cache=True: isi tetap terbaca bila Odoo mengulang permintaan (mis. konflik serialisasi)
        raw = request.httprequest.get_data(cache=True)
        if not request.db:
            return request.make_json_response({"ok": False, "error": "no database"}, status=400)
        env = request.env(su=True)
        connection = env["coretax.connection"].browse(connection_id).exists()
        if not connection or not connection.webhook_secret:
            return request.make_json_response({"ok": False, "error": "unknown endpoint"}, status=404)
        headers = request.httprequest.headers
        header = headers.get("X-PajakMu-Signature") or headers.get("AksaTax-Signature", "")
        if not verify_signature(connection.webhook_secret, header, raw):
            _logger.warning("Webhook PajakMu ditolak: tanda tangan tidak valid (koneksi %s)", connection_id)
            return request.make_json_response({"ok": False, "error": "invalid signature"}, status=401)
        try:
            event = json.loads(raw or b"{}")
        except ValueError:
            return request.make_json_response({"ok": False, "error": "invalid json"}, status=400)
        # auth='none' tidak punya perusahaan aktif; with_company() Odoo 20 butuh allowed_company_ids
        connection = connection.with_context(allowed_company_ids=connection.company_id.ids)
        handled = connection._process_event(event)
        env["coretax.log"].create({
            "connection_id": connection.id, "company_id": connection.company_id.id,
            "operation": f"Webhook diterima: {event.get('type') or '-'}", "method": "POST",
            "path": request.httprequest.path, "status_code": 200, "success": True,
            "request_id": headers.get("X-PajakMu-Delivery") or headers.get("AksaTax-Delivery", ""),
            "message": "Diproses" if handled else "Diabaikan (dokumen bukan dari Odoo)",
            "response": raw.decode(errors="replace")[:8000],
        })
        return request.make_json_response({"ok": True, "handled": bool(handled)})
