/**
 * Alpine requestTable — filterable/sortable request list.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('requestTable', () => ({
        requests: [],
        loading: true,
        error: null,
        filterStatus: '',
        filterCampaign: '',
        sortCol: 'updated_at',
        sortAsc: false,

        init() {
            this.fetchData();
            window.addEventListener('wms2:refresh', () => this.fetchData());
        },

        async fetchData() {
            try {
                this.loading = true;
                this.error = null;
                const params = new URLSearchParams();
                if (this.filterStatus) params.set('status', this.filterStatus);
                params.set('limit', '500');
                this.requests = await WMS2_API.listRequests(params.toString());
            } catch (e) {
                this.error = e.message;
            } finally {
                this.loading = false;
            }
        },

        get filtered() {
            let list = this.requests;
            if (this.filterCampaign) {
                const q = this.filterCampaign.toLowerCase();
                list = list.filter(r => (r.campaign || '').toLowerCase().includes(q)
                    || (r.request_name || '').toLowerCase().includes(q));
            }
            list = [...list].sort((a, b) => {
                let va = a[this.sortCol], vb = b[this.sortCol];
                if (va == null) va = '';
                if (vb == null) vb = '';
                if (typeof va === 'string') va = va.toLowerCase();
                if (typeof vb === 'string') vb = vb.toLowerCase();
                if (va < vb) return this.sortAsc ? -1 : 1;
                if (va > vb) return this.sortAsc ? 1 : -1;
                return 0;
            });
            return list;
        },

        sort(col) {
            if (this.sortCol === col) {
                this.sortAsc = !this.sortAsc;
            } else {
                this.sortCol = col;
                this.sortAsc = true;
            }
        },

        sortIcon(col) {
            if (this.sortCol !== col) return '';
            return this.sortAsc ? ' \u25B2' : ' \u25BC';
        },

        applyStatusFilter() {
            this.fetchData();
        },
    }));
});
