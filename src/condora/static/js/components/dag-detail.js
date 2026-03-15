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
        performance: null,
        loading: true,
        error: null,

        // Sort/filter state per table
        wuSort: { key: 'name', dir: 1 },
        wuFilter: '',
        jobSort: { key: 'work_unit', dir: 1 },
        jobFilter: '',
        histSort: { key: null, dir: 1 },
        histFilter: '',

        init() {
            this.fetchAll();
            window.addEventListener('condora:refresh', () => this.fetchAll());
        },

        async fetchAll() {
            try {
                this.loading = true;
                this.error = null;
                const [d, h, j, nl, perf] = await Promise.all([
                    CONDORA_API.getDAG(this.dagId),
                    CONDORA_API.getDAGHistory(this.dagId).catch(() => []),
                    CONDORA_API.getDAGJobs(this.dagId).catch(() => []),
                    CONDORA_API.getDAGNodeLog(this.dagId).catch(() => []),
                    CONDORA_API.getDAGPerformance(this.dagId).catch(() => null),
                ]);
                this.dag = d;
                this.history = h;
                this.jobs = j;
                this.nodeLog = nl;
                this.performance = perf;
            } catch (e) {
                this.error = e.message;
            } finally {
                this.loading = false;
            }
        },

        // ── Generic sort/filter helpers ──

        /** Toggle sort on a table. */
        toggleSort(sortState, key) {
            if (sortState.key === key) {
                sortState.dir *= -1;
            } else {
                sortState.key = key;
                sortState.dir = 1;
            }
        },

        /** Sort indicator for a column header. */
        sortIcon(sortState, key) {
            if (sortState.key !== key) return '';
            return sortState.dir === 1 ? ' \u25B2' : ' \u25BC';
        },

        /** Filter rows: any field value contains the query (case-insensitive). */
        _matchesFilter(row, query) {
            if (!query) return true;
            const q = query.toLowerCase();
            for (const v of Object.values(row)) {
                if (v != null && String(v).toLowerCase().includes(q)) return true;
            }
            return false;
        },

        /** Sort an array by key, handling numbers and strings. */
        _sorted(arr, key, dir) {
            if (!key) return arr;
            return [...arr].sort((a, b) => {
                let va = a[key], vb = b[key];
                if (va == null && vb == null) return 0;
                if (va == null) return 1;
                if (vb == null) return -1;
                if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
                return String(va).localeCompare(String(vb)) * dir;
            });
        },

        // ── Node log ──

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
            const s = Math.floor(secs % 60);
            if (h > 0) return h + 'h ' + String(m).padStart(2, '0') + 'm';
            if (m > 0) return m + 'm ' + String(s).padStart(2, '0') + 's';
            return s + 's';
        },

        /** Format chirp-based step progress for a job. */
        fmtStepProgress(j) {
            if (!j.current_step) return null;
            if (j.phase === 'stageout') {
                let label = 'stageout';
                if (j.stageout_start && j.wall_time != null) {
                    const now = Math.floor(Date.now() / 1000);
                    const elapsed = now - j.stageout_start;
                    if (elapsed > 0) label += ' (' + this.fmtWallTime(elapsed) + ')';
                }
                return label;
            }
            let s = 'Step ' + j.current_step;
            if (j.num_steps) s += '/' + j.num_steps;
            if (j.step_name) s += ' ' + j.step_name;
            return s;
        },

        // ── Performance ──

        get hasPerformance() {
            if (!this.performance) return false;
            return this.performance.dag_metrics.length > 0 ||
                (this.performance.step_data && this.performance.step_data.wu_metrics);
        },

        get roundStepMetrics() {
            if (!this.performance || !this.performance.step_data) return [];
            const sd = this.performance.step_data;
            const wuMetrics = sd.wu_metrics;
            if (!Array.isArray(wuMetrics) || wuMetrics.length === 0) return [];
            const stepAccum = {};
            for (const wu of wuMetrics) {
                const perStep = wu.per_step;
                if (!perStep) continue;
                for (const [stepNum, s] of Object.entries(perStep)) {
                    if (!stepAccum[stepNum]) {
                        stepAccum[stepNum] = { n: 0, cpu: 0, rss: 0, vsize: 0, tpe: 0, thr: 0, wall: 0,
                            read: 0, write: 0, evProc: 0, evWrit: 0 };
                    }
                    const a = stepAccum[stepNum];
                    const nj = s.num_jobs || 1;
                    a.n += nj;
                    if (s.cpu_efficiency?.mean != null) a.cpu += s.cpu_efficiency.mean * nj;
                    if (s.peak_rss_mb?.mean != null) a.rss += s.peak_rss_mb.mean * nj;
                    if (s.peak_vsize_mb?.mean != null) a.vsize += s.peak_vsize_mb.mean * nj;
                    if (s.time_per_event_sec?.mean != null) a.tpe += s.time_per_event_sec.mean * nj;
                    if (s.throughput_ev_s?.mean != null) a.thr += s.throughput_ev_s.mean * nj;
                    if (s.wall_time_sec?.mean != null) a.wall += s.wall_time_sec.mean * nj;
                    if (s.read_mb?.mean != null) a.read += s.read_mb.mean * nj;
                    if (s.write_mb?.mean != null) a.write += s.write_mb.mean * nj;
                    if (s.events_processed?.total != null) a.evProc += s.events_processed.total;
                    if (s.events_written?.total != null) a.evWrit += s.events_written.total;
                }
            }
            return Object.entries(stepAccum)
                .sort(([a], [b]) => Number(a) - Number(b))
                .map(([stepNum, a]) => ({
                    step: stepNum,
                    num_jobs: a.n,
                    cpu_efficiency: a.n ? (a.cpu / a.n * 100).toFixed(1) : '—',
                    peak_rss_mb: a.n ? (a.rss / a.n).toFixed(0) : '—',
                    peak_vsize_mb: a.n ? (a.vsize / a.n).toFixed(0) : '—',
                    time_per_event: a.n ? (a.tpe / a.n).toFixed(2) : '—',
                    throughput: a.n ? (a.thr / a.n).toFixed(4) : '—',
                    wall_time: a.n ? (a.wall / a.n).toFixed(0) : '—',
                    read_mb: a.n ? (a.read / a.n).toFixed(1) : '—',
                    write_mb: a.n ? (a.write / a.n).toFixed(1) : '—',
                    events_processed: a.evProc || '—',
                    events_written: a.evWrit || '—',
                }));
        },

        // ── Node counts & WU progress ──

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

        jumpToNodeLog(nodeName) {
            this.showNodeLog = true;
            this.nodeFilter = nodeName;
            this.$nextTick(() => {
                const el = document.querySelector('[data-section="node-event-log"]');
                if (el) el.scrollIntoView({ behavior: 'smooth' });
            });
        },

        get nodeCountsByType() {
            if (!this.dag || !this.dag.node_counts) return [];
            const nc = this.dag.node_counts;
            const order = ['processing', 'landing', 'merge', 'cleanup'];
            return order
                .filter(t => nc[t] != null && nc[t] > 0)
                .map(t => ({ type: t.charAt(0).toUpperCase() + t.slice(1), count: nc[t] }));
        },

        // ── WU summary (sortable + filterable) ──

        /** Aggregate jobs by work_unit for the WU summary table. */
        get _wuSummaryRaw() {
            if (!this.jobs || this.jobs.length === 0) return [];
            const wuMap = {};
            for (const j of this.jobs) {
                const wu = j.work_unit || '';
                if (!wu) continue;
                if (!wuMap[wu]) {
                    wuMap[wu] = { name: wu, running: 0, idle: 0, held: 0, site: null,
                                  wall_sum: 0, wall_n: 0, cpu_sum: 0, cpu_n: 0, mem_sum: 0, mem_n: 0 };
                }
                const w = wuMap[wu];
                if (j.status === 'running') w.running++;
                else if (j.status === 'idle') w.idle++;
                else if (j.status === 'held') w.held++;
                if (j.site) w.site = j.site;
                if (j.wall_time != null) { w.wall_sum += j.wall_time; w.wall_n++; }
                if (j.cpu_efficiency != null) { w.cpu_sum += j.cpu_efficiency; w.cpu_n++; }
                if (j.memory_mb != null) { w.mem_sum += j.memory_mb; w.mem_n++; }
            }
            return Object.values(wuMap).map(w => ({
                ...w,
                avg_wall: w.wall_n ? Math.round(w.wall_sum / w.wall_n) : null,
                avg_cpu_eff: w.cpu_n ? (w.cpu_sum / w.cpu_n).toFixed(1) : null,
                avg_mem: w.mem_n ? Math.round(w.mem_sum / w.mem_n) : null,
            }));
        },

        get wuSummary() {
            let data = this._wuSummaryRaw.filter(r => this._matchesFilter(r, this.wuFilter));
            return this._sorted(data, this.wuSort.key, this.wuSort.dir);
        },

        // ── Jobs (sortable + filterable) ──

        get filteredJobs() {
            let data = this.jobs.filter(r => this._matchesFilter(r, this.jobFilter));
            return this._sorted(data, this.jobSort.key, this.jobSort.dir);
        },

        // ── History (sortable + filterable) ──

        get filteredHistory() {
            let data = this.history.filter(r => this._matchesFilter(r, this.histFilter));
            return this._sorted(data, this.histSort.key, this.histSort.dir);
        },
    }));
});
