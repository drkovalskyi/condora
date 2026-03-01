/**
 * Alpine requestDetail — fetches and exposes request + workflow + DAG data.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('requestDetail', (requestName) => ({
        name: requestName,
        request: null,
        workflow: null,
        dag: null,
        errors: null,
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
                this.request = await WMS2_API.getRequest(this.name);

                // Fetch workflow, DAG, and errors in parallel (graceful failures)
                const [wf, errs] = await Promise.all([
                    WMS2_API.getWorkflowByRequest(this.name).catch(() => null),
                    WMS2_API.getRequestErrors(this.name).catch(() => null),
                ]);
                this.workflow = wf;
                this.errors = errs;

                if (wf && wf.dag_id) {
                    this.dag = await WMS2_API.getDAG(wf.dag_id).catch(() => null);
                } else {
                    this.dag = null;
                }
            } catch (e) {
                this.error = e.message;
            } finally {
                this.loading = false;
            }
        },

        get hasWorkflow() { return this.workflow != null; },
        get hasDAG() { return this.dag != null; },
        get hasErrors() {
            if (!this.errors) return false;
            const e = this.errors;
            return (e.total_failures || 0) > 0 || (e.site_errors && e.site_errors.length > 0);
        },

        get progressPct() {
            if (!this.workflow) return 0;
            return this.workflow.progress_pct || 0;
        },

        get stepMetrics() {
            if (!this.workflow || !this.workflow.step_metrics) return [];
            const sm = this.workflow.step_metrics;
            if (Array.isArray(sm)) return sm;
            // step_metrics might be an object keyed by step name
            return Object.entries(sm).map(([step, data]) => ({ step, ...data }));
        },

        get transitions() {
            if (!this.request || !this.request.status_transitions) return [];
            return this.request.status_transitions;
        },
    }));
});
