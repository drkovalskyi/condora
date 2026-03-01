/**
 * Alpine workflowDetail — fetches and exposes workflow, blocks, and DAG data.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('workflowDetail', (workflowId) => ({
        workflowId: workflowId,
        workflow: null,
        blocks: [],
        dag: null,
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
                this.workflow = await WMS2_API.getWorkflow(this.workflowId);

                const [blks] = await Promise.all([
                    WMS2_API.getWorkflowBlocks(this.workflowId).catch(() => []),
                ]);
                this.blocks = blks;

                if (this.workflow.dag_id) {
                    this.dag = await WMS2_API.getDAG(this.workflow.dag_id).catch(() => null);
                } else {
                    this.dag = null;
                }
            } catch (e) {
                this.error = e.message;
            } finally {
                this.loading = false;
            }
        },

        get progressPct() {
            if (!this.workflow) return 0;
            return this.workflow.progress_pct || 0;
        },

        get stepMetrics() {
            if (!this.workflow || !this.workflow.step_metrics) return [];
            const sm = this.workflow.step_metrics;
            if (Array.isArray(sm)) return sm;
            return Object.entries(sm).map(([step, data]) => ({ step, ...data }));
        },

        get splittingDisplay() {
            if (!this.workflow) return {};
            const sp = this.workflow.splitting_params || {};
            return sp;
        },
    }));
});
