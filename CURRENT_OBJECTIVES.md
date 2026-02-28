# Current Objectives

Main objective: make sure we can process a real workflow (cmsunified_task_BPH-RunIISummer20UL18GEN-00292__v1_T_250801_104414_1441) using WMS2 in a fully automated mode from scratch using default splitting. To speed things up we are using test fraction of 0.01.

## Status

Fixed 6 error handling bugs and verified them against a live run with broken siteconf. All 6 fixes confirmed working. Siteconf restored. Next step: clean run of the workflow end-to-end.

## Command

```bash
wms2 import cmsunified_task_BPH-RunIISummer20UL18GEN-00292__v1_T_250801_104414_1441 \
  --sandbox-mode cmssw \
  --test-fraction 0.01
```

## Known Issues (fixed)

1. Wrong failure ratio — was using inner node count instead of work units
2. `read_post_data()` missed early-aborted nodes (filtered on `final=True`)
3. Rescue DAG submission crashed — no `Force` option for `from_dag()`
4. 2-node sub-DAGs couldn't reach early abort threshold (hardcoded at 3)
5. Infrastructure errors retried same broken site 3x with 300s cooloff
6. Rescue DAG landed on same broken site — no site exclusion mechanism

## Potential blockers (not yet verified)

- Whether the full end-to-end flow works after these fixes
- Other failure modes that may surface with real data

## After every failure

Review how error handling performed: check POST script exit codes, retry behavior, early abort, failure ratio computation, and final request status. Confirm no time was wasted on unnecessary retries. If error handling misbehaved, fix it before re-running.

## Definition of done

Workflow completes end-to-end with all outputs registered, no manual intervention needed.
