# WMS2 Planning

<!-- Everything above and including the "---" divider is human-owned; Claude must not edit it.
     Everything below the "---" is Claude-owned; update as work progresses. -->

## Objectives

Main objective: make sure we can process a real workflow using WMS2 in
a fully automated mode from scratch using default splitting. The first
round should have only one work unit created automatically, which
measures optimal parameters for the jobs. Next round should
optimize. Keep track of all issues starting and running the test - we
need to fix them.

Workflow to use: cmsunified_task_B2G-Run3Summer23BPixwmLHEGS-06000__v1_T_250628_211038_1313

To speed things up we are using test fraction of 0.01.

### Service mode

Build the service that manages requests autonomously:

- Requests are injected into WMS2 DB (via API or import tool)
- Lifecycle manager service runs continuously, discovers active requests,
  polls DAGs, handles round transitions, adaptive optimization
- CLI becomes a thin client: import + optionally tail logs
- The lifecycle manager already has the core logic; needs to be wired into
  a long-running service loop

### Monitoring

Build observability for WMS2:

- Dashboard showing active requests, workflow status, round progress
- Per-workflow metrics: events_produced vs target, current round, job counts
- Per-step performance: CPU efficiency, memory usage, throughput
- Alerting on stuck/failed workflows
- HTCondor queue overview (running/idle/held by workflow)
- Technology TBD (Prometheus + Grafana, or simple web UI, or CLI status command)

## Future improvements (not fixing now)

- **Pileup (secondary input) site selection** — configure CMSSW to prefer
  local/nearby replicas or provide a site-filtered pileup file list
- **Intra-DAG replan nodes** — replan between WU0 and WU1 within a single DAG
- **Probe nodes** — modified last proc node in WU0 for memory measurement
- **Pipeline split mode** — code moved but not wired in yet

## After every failure

Review how error handling performed: check POST script exit codes,
retry behavior, early abort, failure ratio computation, and final
request status. Confirm no time was wasted on unnecessary retries. If
error handling misbehaved, fix it before re-running.

---

## Claude Status

### Current status

CLI-based end-to-end processing works for a real 5-step StepChain workflow.
Matrix smoke tests (5 workflows including fault injection) all pass.

### Verified working

- Import from ReqMgr2, DAG planning, job submission, processing (5-step StepChain)
- Seed randomization (each job gets a unique random seed)
- Merge (AODSIM, MINIAODSIM, NANOAODSIM via cmsRun)
- Cleanup of unmerged files after merge
- Round completion with metrics aggregation, adaptive optimization, events tracking
- Error handling: retry, rescue DAG, early abort, site exclusion
- Shared `complete_round()` logic between CLI and lifecycle manager

### Test commands

Real CMSSW workflow:
```bash
wms2 import cmsunified_task_B2G-Run3Summer23BPixwmLHEGS-06000__v1_T_250628_211038_1313 \
  --sandbox-mode cmssw \
  --test-fraction 0.01
```

Matrix smoke tests:
```bash
python -m tests.matrix -l smoke
```

### Known issues

- NanoAOD Rivet segfault on 0 events (CMSSW_10_6_47 bug, not WMS2)

### Historical issues (fixed)

1. Wrong failure ratio — was using inner node count instead of work units
2. `read_post_data()` missed early-aborted nodes (filtered on `final=True`)
3. Rescue DAG submission crashed — no `Force` option for `from_dag()`
4. 2-node sub-DAGs couldn't reach early abort threshold (hardcoded at 3)
5. Infrastructure errors retried same broken site 3x with 300s cooloff
6. Rescue DAG landed on same broken site — no site exclusion mechanism
7. SplittingParams key mismatch — `EventsPerJob` lowercased to `eventsperjob` instead of `events_per_job`
8. Missing filter_efficiency — StepChain Step1.FilterEfficiency not used when top-level absent
9. `set -euo pipefail` killed script before STEP_RC captured
10. GenFilter events_per_job double-inflation
11. Premature completion — offset-based termination vs production-based
12. Merge job not merging — manifest.json missing from transfer_input_files
13. Duplicate physics events — seeds not randomized
14. events_produced=0 after round completion — fixed with disk fallback
15. step_metrics=NULL — now stores WU performance data
16. Cleanup job can't find cleanup_manifest.json — not in transfer_input_files
17. Matrix mock missing adaptive fields — MagicMock returned mocks instead of ints
18. CLI duplicated round-completion logic — refactored to shared `complete_round()`
