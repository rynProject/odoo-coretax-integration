{
    "name": "Coretax Integration (AksaTax)",
    "summary": "Kirim e-Faktur ke DJP (Coretax) lewat AksaTax, PPh di tagihan vendor otomatis jadi bukti potong, status kembali ke Odoo.",
    "description": """
Coretax Integration (AksaTax)
=============================
Menghubungkan Odoo dengan AksaTax (https://aksatax.id) melalui Public API-nya:

* e-Faktur Keluaran: dari faktur pelanggan Odoo -> draft di AksaTax -> kirim ke
  DJP; Nomor Faktur Pajak, kode persetujuan, dan status DJP kembali ke Odoo
  otomatis (polling setiap 5 menit atau webhook bertanda tangan HMAC).
* Pemeriksaan sebelum kirim dengan bahasa yang mudah dipahami (NPWP/NIK
  pelanggan, kode transaksi, kode barang/jasa, mata uang, kelompok pajak).
* Bukti Potong PPh Unifikasi (BPPU/BPNR) dari tagihan vendor, dikirim ke
  AksaTax sebagai draft untuk ditandatangani di sana.
* PPh pada tagihan vendor: pajak PPh (mis. PPh 23, PPh 4 ayat 2) per kode objek
  dengan akun terpisah, serta bukti potong otomatis per kode objek saat
  tagihan diposting, tetap sinkron saat tagihan dibatalkan atau dibuatkan nota kredit.
* Pencocokan pelanggan/vendor dengan Lawan Transaksi AksaTax (per perusahaan).
* Ringkasan, daftar "Perlu tindakan", dan Riwayat Sinkronisasi.

Data baris e-Faktur memakai penyusun data bawaan Odoo
(l10n_id_efaktur_coretax), jadi angkanya sama dengan XML Coretax resmi.
""",
    "version": "19.0.1.2.0",
    "category": "Accounting/Localizations/EDI",
    "author": "Aksa",
    "website": "https://www.aksa.id",
    "license": "LGPL-3",
    "depends": ["account", "mail", "l10n_id_efaktur_coretax"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/coretax_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "data/coretax_tax_object_data.xml",
        "views/coretax_connection_views.xml",
        "views/coretax_faktur_views.xml",
        "views/coretax_bupot_views.xml",
        "views/coretax_log_views.xml",
        "views/coretax_wizard_views.xml",
        "views/account_move_views.xml",
        "views/account_tax_views.xml",
        "views/res_partner_views.xml",
        "views/res_config_settings_views.xml",
        "views/coretax_readiness_views.xml",
        "views/coretax_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "coretax_integration/static/src/dashboard/coretax_dashboard.js",
            "coretax_integration/static/src/dashboard/coretax_dashboard.xml",
            "coretax_integration/static/src/dashboard/coretax_dashboard.scss",
        ],
    },
    "images": ["static/description/banner.png", "static/description/dashboard_screenshot.png"],
    "application": True,
    "installable": True,
    "post_init_hook": "_coretax_post_init",
}
