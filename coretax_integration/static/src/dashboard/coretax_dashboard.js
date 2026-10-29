/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { formatCurrency } from "@web/core/currency";
import { deserializeDate, deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

// Ringkasan Coretax: satu layar untuk pengguna non-teknis - apa yang perlu
// dikirim, apa yang sedang diproses DJP, dan apa yang perlu diperbaiki.
const STATE_TONE = {
    approved: "success",
    rejected: "danger",
    error: "danger",
    submitted: "info",
    processing: "info",
    draft: "warning",
    pending_approval: "warning",
    validated: "warning",
    cancel_requested: "muted",
    cancelled: "muted",
};

export class CoretaxDashboard extends Component {
    static template = "coretax_integration.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ data: null, syncing: false });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.data = await this.orm.call("coretax.faktur", "get_dashboard_data", []);
    }

    get d() {
        return this.state.data;
    }

    get connected() {
        return Boolean(this.d.connection && this.d.connection.state === "ok");
    }

    /** ok / error / draft (belum diuji) / none (belum diatur) - untuk titik status di kepala. */
    get connectionTone() {
        const c = this.d.connection;
        if (!c) {
            return "none";
        }
        return c.state === "ok" ? "ok" : c.state === "error" ? "error" : "draft";
    }

    /** Panduan awal tampil sampai koneksi terhubung dan e-Faktur pertama dibuat. */
    get showOnboarding() {
        return !this.connected || !this.d.recent.length;
    }

    get steps() {
        const c = this.d.connection;
        return [
            { done: Boolean(c && c.has_api_key), title: "Isi API key AksaTax",
              sub: "Pengaturan › Koneksi AksaTax. Salin API key dari AksaTax › Integrasi." },
            { done: this.connected, title: "Uji koneksi", sub: "Klik Uji koneksi sampai statusnya Terhubung." },
            { done: this.d.counts.partners_unmatched === 0, title: "Cocokkan pelanggan",
              sub: "Tautkan pelanggan ber-NPWP ke Lawan Transaksi AksaTax." },
            { done: this.d.recent.length > 0, title: "Kirim e-Faktur pertama",
              sub: "e-Faktur › Siap dikirim, pilih faktur lalu Kirim e-Faktur." },
        ];
    }

    /** Tahap e-Faktur sesuai urutan sebenarnya, dari faktur Odoo sampai disetujui DJP. */
    get flow() {
        const c = this.d.counts;
        return [
            { key: "ready", tone: "primary", icon: "fa-inbox", value: c.ready, label: "Siap dikirim",
              hint: "Faktur ber-PPN tanpa e-Faktur", open: () => this.openReady() },
            { key: "draft", tone: "warning", icon: "fa-pencil-square-o", value: c.draft, label: "Draft di AksaTax",
              hint: "Belum dikirim ke DJP", open: () => this.openFaktur("draft") },
            { key: "in_progress", tone: "info", icon: "fa-hourglass-half", value: c.in_progress, label: "Diproses DJP",
              hint: "Menunggu jawaban DJP", open: () => this.openFaktur("in_progress") },
            { key: "approved", tone: "success", icon: "fa-check-circle", value: c.approved_month,
              label: "Disetujui bulan ini", hint: "Nomor Faktur Pajak terbit", open: () => this.openFaktur("approved") },
        ];
    }

    get attentionDetail() {
        const c = this.d.counts;
        const parts = [];
        if (c.rejected) {
            parts.push(`${c.rejected} ditolak DJP`);
        }
        if (c.failed) {
            parts.push(`${c.failed} gagal dikirim`);
        }
        return parts.join(" · ");
    }

    get monthLabel() {
        return deserializeDate(this.d.month.start).toFormat("LLLL yyyy");
    }

    /** Potongan bar komposisi status masa pajak berjalan (lebar dalam persen). */
    get monthBar() {
        const rows = this.d.month.by_state;
        const total = rows.reduce((sum, row) => sum + row.count, 0);
        return rows.map((row) => ({ ...row, tone: this.tone(row.state), width: (row.count / total) * 100 }));
    }

    tone(state) {
        return STATE_TONE[state] || "warning";
    }

    money(amount) {
        return formatCurrency(amount || 0, this.d.currency_id, { digits: [16, 0] });
    }

    when(value) {
        return value ? formatDateTime(deserializeDateTime(value)) : "-";
    }

    /** "5 menit yang lalu" mengikuti bahasa pengguna. */
    ago(value) {
        return value ? deserializeDateTime(value).toRelative() : "-";
    }

    open(xmlid, context = {}) {
        return this.action.doAction(xmlid, { additionalContext: context });
    }

    openReady() {
        return this.open("coretax_integration.action_coretax_invoice_ready");
    }

    openFaktur(filter) {
        return this.open("coretax_integration.action_coretax_faktur", filter ? { [`search_default_${filter}`]: 1 } : {});
    }

    openFakturRecord(id) {
        return this.action.doAction({
            type: "ir.actions.act_window", res_model: "coretax.faktur", res_id: id, views: [[false, "form"]],
        });
    }

    openBupot() {
        return this.open("coretax_integration.action_coretax_bupot", { search_default_todo: 1 });
    }

    openPartners() {
        return this.open("coretax_integration.action_coretax_partner");
    }

    /** Daftar kesiapan data (produk berkode umum, satuan tanpa kode, pelanggan tanpa NPWP/NIK). */
    async openReadiness(kind) {
        const action = await this.orm.call("coretax.faktur", "action_readiness", [kind]);
        return this.action.doAction(action);
    }

    get readiness() {
        const c = this.d.counts;
        return [
            { kind: "partners_no_id", icon: "fa-id-card-o", count: c.partners_no_id, label: "Pelanggan tanpa NPWP/NIK",
              hint: "Ada di faktur yang siap dikirim" },
            { kind: "products_generic", icon: "fa-cube", count: c.products_generic, label: "Produk berkode umum",
              hint: "Masih memakai kode barang/jasa 000000" },
            { kind: "uoms_no_code", icon: "fa-balance-scale", count: c.uoms_no_code, label: "Satuan tanpa kode Coretax",
              hint: "Dipakai produk yang dijual" },
        ];
    }

    openConnection() {
        return this.action.doAction("coretax_integration.action_coretax_connection_open");
    }

    openAksaTax() {
        if (this.d.connection && this.d.connection.url) {
            window.open(this.d.connection.url, "_blank", "noopener");
        }
    }

    async matchAll() {
        if (this.state.syncing) {
            return;
        }
        this.state.syncing = true;
        try {
            const result = await this.orm.call("res.partner", "action_coretax_match_all", [[]]);
            await this.load();
            if (result) {
                await this.action.doAction({ ...result, params: { ...result.params, next: undefined } });
            }
        } finally {
            this.state.syncing = false;
        }
    }

    async syncNow() {
        const c = this.d.connection;
        if (!c || this.state.syncing) {
            return;
        }
        this.state.syncing = true;
        try {
            const result = await this.orm.call("coretax.connection", "action_sync_now", [[c.id]]);
            await this.load();
            if (result) {
                await this.action.doAction({ ...result, params: { ...result.params, next: undefined } });
            }
        } finally {
            this.state.syncing = false;
        }
    }
}

registry.category("actions").add("coretax_dashboard", CoretaxDashboard);
