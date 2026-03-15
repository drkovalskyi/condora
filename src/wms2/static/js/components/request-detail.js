/**
 * Alpine requestDetail — fetches and exposes request + workflow + DAG data.
 * Includes action methods (stop, fail, clone) with toast feedback.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('requestDetail', (requestName) => ({
        name: requestName,
        request: null,
        workflow: null,
        dag: null,
        allDags: [],
        outputDatasets: [],
        errors: null,
        sitePerformance: null,
        loading: true,
        error: null,

        // Action state
        actionLoading: false,
        showStopDialog: false,
        showFailDialog: false,
        showCloneDialog: false,
        showDeleteDialog: false,
        stopReason: 'Operator-initiated clean stop',

        // Priority profile editing
        editingPriority: false,
        prioHigh: 5,
        prioNominal: 3,
        prioSwitchFraction: 0.5,

        init() {
            this.fetchAll();
            window.addEventListener('wms2:refresh', () => this.fetchAll());
        },

        async fetchAll() {
            try {
                this.loading = true;
                this.error = null;

                // Phase 1: fetch request + workflow + DAG (fast — renders the page)
                const [req, wf] = await Promise.all([
                    WMS2_API.getRequest(this.name),
                    WMS2_API.getWorkflowByRequest(this.name).catch(() => null),
                ]);
                this.request = req;
                this.workflow = wf;

                if (wf) {
                    const [dags, ods] = await Promise.all([
                        WMS2_API.getWorkflowDags(wf.id).catch(() => []),
                        WMS2_API.getWorkflowOutputDatasets(wf.id).catch(() => []),
                    ]);
                    // Pick the most relevant DAG to display: prefer running/submitted,
                    // then fall back to the most recently created DAG.
                    const activeDag = dags.find(d => d.status === 'running' || d.status === 'submitted');
                    this.dag = activeDag || (dags.length > 0 ? dags[0] : null);
                    this.allDags = dags;
                    this.outputDatasets = ods;
                } else {
                    this.dag = null;
                    this.allDags = [];
                    this.outputDatasets = [];
                }
            } catch (e) {
                this.error = e.message;
            } finally {
                this.loading = false;
            }

            // Phase 2: fetch errors + site performance in background (slow)
            Promise.all([
                WMS2_API.getRequestErrors(this.name).catch(() => null),
                WMS2_API.getSitePerformance(this.name).catch(() => null),
            ]).then(([errs, sitePerf]) => {
                this.errors = errs;
                this.sitePerformance = sitePerf;
            });
        },

        get hasWorkflow() { return this.workflow != null; },
        get hasDAG() { return this.dag != null; },
        get activeRoundsDisplay() {
            const active = this.allDags
                .filter(d => d.status === 'submitted' || d.status === 'running')
                .map(d => d.round_number)
                .filter(r => r != null);
            if (active.length === 0) {
                return this.workflow ? String(this.workflow.current_round) : '—';
            }
            const sorted = [...new Set(active)].sort((a, b) => a - b);
            if (sorted.length === 1) return String(sorted[0]);
            return sorted[0] + '–' + sorted[sorted.length - 1];
        },
        get hasErrors() {
            if (!this.errors) return false;
            const e = this.errors;
            return (e.total_failures || 0) > 0 || (e.site_errors && e.site_errors.length > 0);
        },

        get hasSitePerformance() {
            return this.sitePerformance && this.sitePerformance.sites && this.sitePerformance.sites.length > 0;
        },

        get progressPct() {
            if (!this.workflow) return 0;
            return this.workflow.progress_pct || 0;
        },

        get stepMetrics() {
            if (!this.workflow || !this.workflow.step_metrics) return [];
            return parseStepMetrics(this.workflow.step_metrics);
        },

        get transitions() {
            if (!this.request || !this.request.status_transitions) return [];
            return [...this.request.status_transitions].reverse();
        },

        // Action visibility helpers
        // STOP: only when there's a running DAG to stop
        get canStop() {
            return this.request && ['active', 'pilot_running'].includes(this.request.status);
        },
        // FAIL: any non-terminal state (implies STOP)
        get canFail() {
            return this.request && ['active', 'pilot_running', 'stopping', 'held', 'partial', 'paused'].includes(this.request.status);
        },
        // CLONE: any non-completed state (implies STOP + FAIL)
        get canClone() {
            return this.request && !['completed'].includes(this.request.status);
        },
        // DELETE: only terminal failed/aborted
        get canDelete() {
            return this.request && ['failed', 'aborted'].includes(this.request.status);
        },
        get hasActions() {
            return this.canStop || this.canFail || this.canClone || this.canDelete;
        },

        get testFraction() {
            const cd = (this.workflow && this.workflow.config_data) || {};
            const tf = cd.test_fraction;
            return (tf && tf < 1) ? tf : null;
        },

        get processingVersion() {
            // Try workflow config_data first, fall back to request_data
            const cd = (this.workflow && this.workflow.config_data) || {};
            if (cd.processing_version) return cd.processing_version;
            const rd = (this.request && this.request.request_data) || {};
            return rd.ProcessingVersion || rd.processing_version || null;
        },

        get workUnitsPerRound() {
            const cd = (this.workflow && this.workflow.config_data) || {};
            return cd.work_units_per_round || null;
        },

        get stageoutMode() {
            const cd = (this.workflow && this.workflow.config_data) || {};
            return cd.stageout_mode || null;
        },

        get consolidationRse() {
            const cd = (this.workflow && this.workflow.config_data) || {};
            return cd.consolidation_rse || null;
        },

        get stepsInfo() {
            const cd = (this.workflow && this.workflow.config_data) || {};
            const steps = cd.manifest_steps || [];
            return steps.map((s, i) => ({
                index: i + 1,
                name: s.name || `Step ${i + 1}`,
                cmssw: s.cmssw_version || '—',
                global_tag: s.global_tag || '—',
                scram_arch: s.scram_arch || '—',
            }));
        },

        get priorityProfile() {
            const rd = (this.request && this.request.request_data) || {};
            const pp = rd._priority_profile;
            if (pp) return pp;
            // Fall back to workflow config_data
            const cd = (this.workflow && this.workflow.config_data) || {};
            return cd.priority_profile || null;
        },

        get currentJobPriority() {
            const rd = (this.request && this.request.request_data) || {};
            return rd._current_job_priority;
        },

        startEditPriority() {
            const pp = this.priorityProfile || {};
            this.prioHigh = pp.high != null ? pp.high : 5;
            this.prioNominal = pp.nominal != null ? pp.nominal : 3;
            this.prioSwitchFraction = pp.switch_fraction != null ? pp.switch_fraction : 0.5;
            this.editingPriority = true;
        },

        async savePriority() {
            this.actionLoading = true;
            try {
                await WMS2_API.updatePriorityProfile(this.name, {
                    high: parseInt(this.prioHigh),
                    nominal: parseInt(this.prioNominal),
                    switch_fraction: parseFloat(this.prioSwitchFraction),
                });
                this.toast('success', 'Priority profile updated');
                this.editingPriority = false;
                await this.fetchAll();
            } catch (e) {
                this.toast('error', 'Failed to update priority: ' + e.message);
            } finally {
                this.actionLoading = false;
            }
        },

        // Toast helper
        toast(type, message) {
            window.dispatchEvent(new CustomEvent('wms2:toast', {
                detail: { type, message }
            }));
        },

        // Actions
        async doStop() {
            this.actionLoading = true;
            try {
                const result = await WMS2_API.stopRequest(this.name, this.stopReason);
                this.toast('success', result.message);
                this.showStopDialog = false;
                this.stopReason = 'Operator-initiated clean stop';
                await this.fetchAll();
            } catch (e) {
                this.toast('error', 'Stop failed: ' + e.message);
            } finally {
                this.actionLoading = false;
            }
        },

        async doFail() {
            this.actionLoading = true;
            try {
                const result = await WMS2_API.failRequest(this.name);
                this.toast('success', result.message);
                this.showFailDialog = false;
                await this.fetchAll();
            } catch (e) {
                this.toast('error', 'Fail failed: ' + e.message);
            } finally {
                this.actionLoading = false;
            }
        },

        async doClone() {
            this.actionLoading = true;
            try {
                const result = await WMS2_API.cloneRequest(this.name);
                this.toast('success', 'Cloned: v' + result.cloned_from_version + ' → v' + result.new_version);
                this.showCloneDialog = false;
                await this.fetchAll();
            } catch (e) {
                this.toast('error', 'Clone failed: ' + e.message);
            } finally {
                this.actionLoading = false;
            }
        },

        async doDelete() {
            this.actionLoading = true;
            try {
                const result = await WMS2_API.deleteRequest(this.name);
                this.toast('success', result.message);
                this.showDeleteDialog = false;
                window.location.href = '/ui/';
            } catch (e) {
                this.toast('error', 'Delete failed: ' + e.message);
            } finally {
                this.actionLoading = false;
            }
        },
    }));
});
