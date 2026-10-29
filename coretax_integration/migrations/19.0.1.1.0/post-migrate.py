from odoo import SUPERUSER_ID, api

from odoo.addons.coretax_integration import _coretax_post_init


def migrate(cr, version):
    # post_init_hook hanya jalan saat instalasi; instalasi lama perlu pajak PPh bawaan juga.
    # Fungsinya idempoten (hanya memuat yang belum ada).
    _coretax_post_init(api.Environment(cr, SUPERUSER_ID, {}))
