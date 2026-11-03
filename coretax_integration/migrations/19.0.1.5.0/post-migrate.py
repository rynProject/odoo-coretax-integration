"""Rebranding AksaTax → PajakMu untuk instalasi lama.

- Nama koneksi (field compute tersimpan) dihitung ulang: "AksaTax – PT …" → "PajakMu – PT …".
- Cron ir_cron_coretax_sync bernoupdate="1", jadi namanya tidak ikut berubah saat upgrade; diganti di sini.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    connections = env["coretax.connection"].with_context(active_test=False).search([])
    connections._compute_name()
    cron = env.ref("coretax_integration.ir_cron_coretax_sync", raise_if_not_found=False)
    if cron:
        cron.name = "Coretax: ambil status terbaru dari PajakMu"
    env.flush_all()
