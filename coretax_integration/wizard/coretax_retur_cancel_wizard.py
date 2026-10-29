import hashlib

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.coretax_retur import REMOTE_RETUR_STATES
from ..tools.client import AksaTaxError

MIN_ALASAN = 5


class CoretaxReturCancelWizard(models.TransientModel):
    """Minta pembatalan nota retur yang sudah disetujui DJP, lengkap dengan alasan."""

    _name = "coretax.retur.cancel.wizard"
    _description = "Batalkan nota retur"

    retur_id = fields.Many2one("coretax.retur", string="Nota retur", required=True, ondelete="cascade")
    alasan = fields.Char(string="Alasan pembatalan", required=True)

    def action_confirm(self):
        self.ensure_one()
        retur = self.retur_id
        alasan = (self.alasan or "").strip()
        if len(alasan) < MIN_ALASAN:
            raise UserError(_("Alasan pembatalan minimal %s karakter. Jelaskan singkat kenapa nota retur dibatalkan.",
                              MIN_ALASAN))
        if retur.state != "approved" or not retur.aksatax_id:
            raise UserError(_("Hanya nota retur yang sudah disetujui DJP yang bisa dibatalkan."))
        connection = self.env["coretax.connection"]._for_company(retur.company_id)
        digest = hashlib.sha1(alasan.encode()).hexdigest()[:10]
        key = f"{retur._idempotency_base()}-batal-{retur.cancel_attempt + 1}-{digest}"
        try:
            result = connection._client(retur).cancel_retur(retur.aksatax_id, alasan, key)
        except AksaTaxError as exc:
            retur.write({"last_error": exc.full_message()})
            raise UserError(exc.full_message()) from exc
        retur.write({
            "state": REMOTE_RETUR_STATES.get((result.get("status") or "").upper(), "cancel_requested"),
            "last_error": False, "alasan_batal": alasan, "cancel_attempt": retur.cancel_attempt + 1,
        })
        message = _("Pembatalan nota retur diajukan ke DJP. Alasan: %s", alasan)
        retur.message_post(body=message)
        retur.move_id.message_post(body=message)
        return {"type": "ir.actions.act_window_close"}
