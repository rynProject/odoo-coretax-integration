import json
import logging

from odoo import http
from odoo.http import request

from ..tools.signature import verify_signature

_logger = logging.getLogger(__name__)


class CoretaxWebhookController(http.Controller):
    """Penerima webhook AksaTax: POST /coretax/webhook/<id koneksi>.

    Tanpa sesi Odoo (auth='none'); keasliannya dijamin tanda tangan HMAC
    ``AksaTax-Signature`` dengan Webhook Secret koneksi. Jawaban 2xx berarti
    diterima - AksaTax mengulang kiriman yang gagal (maks. 5 kali)."""

    @http.route("/coretax/webhook/<int:connection_id>", type="http", auth="none", methods=["POST"], csrf=False,
                save_session=False)
    def receive(self, connection_id, **_kwargs):
        raw = request.httprequest.get_data(cache=False)
        if not request.db:
            return request.make_json_response({"ok": False, "error": "no database"}, status=400)
        env = request.env(su=True)
        connection = env["coretax.connection"].browse(connection_id).exists()
        if not connection or not connection.webhook_secret:
            return request.make_json_response({"ok": False, "error": "unknown endpoint"}, status=404)
        header = request.httprequest.headers.get("AksaTax-Signature", "")
        if not verify_signature(connection.webhook_secret, header, raw):
            _logger.warning("Webhook AksaTax ditolak: tanda tangan tidak valid (koneksi %s)", connection_id)
            return request.make_json_response({"ok": False, "error": "invalid signature"}, status=401)
        try:
            event = json.loads(raw or b"{}")
        except ValueError:
            return request.make_json_response({"ok": False, "error": "invalid json"}, status=400)
        connection = connection.with_company(connection.company_id)
        handled = connection._process_event(event)
        env["coretax.log"].create({
            "connection_id": connection.id, "company_id": connection.company_id.id,
            "operation": f"Webhook diterima: {event.get('type') or '-'}", "method": "POST",
            "path": request.httprequest.path, "status_code": 200, "success": True,
            "request_id": request.httprequest.headers.get("AksaTax-Delivery", ""),
            "message": "Diproses" if handled else "Diabaikan (dokumen bukan dari Odoo)",
            "response": raw.decode(errors="replace")[:8000],
        })
        return request.make_json_response({"ok": True, "handled": bool(handled)})
