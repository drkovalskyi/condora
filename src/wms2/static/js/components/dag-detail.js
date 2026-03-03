/**
 * Alpine dagDetail — fetches and exposes DAG detail + history.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('dagDetail', (dagId) => ({
        dagId: dagId,
        dag: null,
        history: [],
        jobs: [],
        nodeLog: [],
        showNodeLog: false,
        nodeFilter: '',
        loading: true,
        error: null,

        init() {
            this.fetchAll();
            window.addEventListener('wms2:refresh', () => this.fetchAll());
        },

        async fetchAll() {
            try {
                this.loading = true;
                this.error = null;
                const [d, h, j, nl] = await Promise.all([
                    WMS2_API.getDAG(this.dagId),
                    WMS2_API.getDAGHistory(this.dagId).catch(() => []),
                    WMS2_API.getDAGJobs(this.dagId).catch(() => []),
                    WMS2_API.getDAGNodeLog(this.dagId).catch(() => []),
                ]);
                this.dag = d;
                this.history = h;
                this.jobs = j;
                this.nodeLog = nl;
            } catch (e) {
                this.error = e.message;
            } finally {
                this.loading = false;
            }
        },

        get nodeLogNodes() {
            const names = new Set(this.nodeLog.map(e => e.node));
            return [...names].sort();
        },

        get filteredNodeLog() {
            const log = this.nodeLog.slice().reverse();
            if (!this.nodeFilter) return log;
            return log.filter(e => e.node === this.nodeFilter);
        },

        eventClass(ev) {
            const ok = { submit: true, execute: true };
            const bad = { held: true, aborted: true, shadow_exception: true };
            const warn = { evicted: true, released: true };
            if (ev.event === 'terminated') {
                return ev.detail.startsWith('exit 0') ? 'event-ok' : 'event-bad';
            }
            if (ev.event === 'post_script') {
                return ev.detail === 'exit 0' ? 'event-ok' : 'event-bad';
            }
            if (bad[ev.event]) return 'event-bad';
            if (warn[ev.event]) return 'event-warn';
            if (ok[ev.event]) return 'event-ok';
            return '';
        },

        fmtWallTime(secs) {
            if (secs == null) return '—';
            const h = Math.floor(secs / 3600);
            const m = Math.floor((secs % 3600) / 60);
            return h > 0 ? h + 'h ' + String(m).padStart(2, '0') + 'm' : m + 'm';
        },

        get totalNodes() {
            return this.dag ? (this.dag.total_nodes || 0) : 0;
        },

        segPct(count) {
            if (!this.totalNodes) return '0%';
            return (100 * (count || 0) / this.totalNodes) + '%';
        },

        get wuCompleted() {
            if (!this.dag) return 0;
            const wu = this.dag.completed_work_units;
            if (Array.isArray(wu)) return wu.length;
            return wu || 0;
        },

        get wuPct() {
            if (!this.dag || !this.dag.total_work_units) return 0;
            return Math.min(100, 100 * this.wuCompleted / this.dag.total_work_units);
        },

        get nodeCountsByType() {
            if (!this.dag || !this.dag.node_counts) return [];
            const nc = this.dag.node_counts;
            const order = ['processing', 'landing', 'merge', 'cleanup'];
            return order
                .filter(t => nc[t] != null && nc[t] > 0)
                .map(t => ({ type: t.charAt(0).toUpperCase() + t.slice(1), count: nc[t] }));
        },
    }));
});
