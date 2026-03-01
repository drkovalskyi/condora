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
            return this.workflow.splitting_params || {};
        },

        get configData() {
            if (!this.workflow) return {};
            return this.workflow.config_data || {};
        },

        get isGen() {
            return !!this.configData._is_gen;
        },

        get pileupDatasets() {
            const steps = this.configData.manifest_steps || [];
            const seen = new Set();
            const result = [];
            for (const s of steps) {
                for (const key of ['mc_pileup', 'data_pileup']) {
                    const ds = s[key];
                    if (ds && !seen.has(ds)) {
                        seen.add(ds);
                        result.push({ type: key === 'mc_pileup' ? 'MC Pileup' : 'Data Pileup', dataset: ds });
                    }
                }
            }
            return result;
        },

        get stepsInfo() {
            const steps = this.configData.manifest_steps || [];
            return steps.map((s, i) => ({
                index: i + 1,
                name: s.name || `Step ${i + 1}`,
                cmssw: s.cmssw_version || '—',
                global_tag: s.global_tag || '—',
                scram_arch: s.scram_arch || '—',
            }));
        },

        get reqmgrUrl() {
            if (!this.workflow) return null;
            return 'https://cmsweb.cern.ch/reqmgr2/fetch?rid=' + encodeURIComponent(this.workflow.request_name);
        },
    }));
});
