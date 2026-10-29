from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.coretax_faktur import REMOTE_STATES
from ..tools.client import AksaTaxError

MIN_ALASAN = 5


class CoretaxFakturCancelWizard(models.TransientModel):
    """Minta pembatalan e-Faktur yang sudah disetujui DJP, lengkap dengan alasan."""

    _name = "coretax.faktur.cancel.wizard"
    _description = "Batalkan e-Faktur"

    faktur_id = fields.Many2one("coretax.faktur", string="e-Faktur", required=True, ondelete="cascade")
    alasan = fields.Char(string="Alasan pembatalan", required=True)

    def action_confirm(self):
        self.ensure_one()
        faktur = self.faktur_id
        alasan = (self.alasan or "").strip()
        if len(alasan) < MIN_ALASAN:
            raise UserError(_("Alasan pembatalan minimal %s karakter. Jelaskan singkat kenapa faktur dibatalkan.",
                              MIN_ALASAN))
        if faktur.state != "approved" or not faktur.aksatax_id:
            raise UserError(_("Hanya e-Faktur yang sudah disetujui DJP yang bisa dibatalkan."))
        connection = self.env["coretax.connection"]._for_company(faktur.company_id)
        try:
            result = connection._client(faktur).cancel_faktur(
                faktur.aksatax_id, alasan, f"{faktur._idempotency_base()}-batal")
        except AksaTaxError as exc:
            faktur.write({"last_error": exc.full_message()})
            raise UserError(exc.full_message()) from exc
        faktur.write({"state": REMOTE_STATES.get(result.get("status"), "cancel_requested"), "last_error": False})
        message = _("Pembatalan e-Faktur diajukan ke DJP. Alasan: %s", alasan)
        faktur.message_post(body=message)
        faktur.move_id.message_post(body=message)
        return {"type": "ir.actions.act_window_close"}
