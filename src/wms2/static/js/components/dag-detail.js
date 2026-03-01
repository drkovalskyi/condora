/**
 * Alpine dagDetail — fetches and exposes DAG detail + history.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('dagDetail', (dagId) => ({
        dagId: dagId,
        dag: null,
        history: [],
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
                const [d, h] = await Promise.all([
                    WMS2_API.getDAG(this.dagId),
                    WMS2_API.getDAGHistory(this.dagId).catch(() => []),
                ]);
                this.dag = d;
                this.history = h;
            } catch (e) {
                this.error = e.message;
            } finally {
                this.loading = false;
            }
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
