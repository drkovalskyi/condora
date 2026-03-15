# Design Proposal: DAG Structure Scalability

## Context

A 500-WU round creates 501 scheduler universe jobs (DAGMan processes) on the
remote schedd. This caused:
- 15+ minute DAGMan startup time parsing 500 sub-DAGs
- Orphaned sub-DAGs blocking new submissions (191 zombies prevented 201401 from starting)
- Schedd memory pressure (~50-100 MB per DAGMan instance x 500 = 25-50 GB)

The SUBDAG EXTERNAL architecture was chosen for site-affine execution and
WU-level atomic retry. The question: can we get these properties without
501 DAGMan processes?

## Current Architecture (SUBDAG EXTERNAL)

```
workflow.dag (1 DAGMan process)
+-- SUBDAG EXTERNAL mg_000000 -> group.dag (1 DAGMan process)
|   +-- landing -> elect_site.sh (POST)
|   +-- proc_000000..proc_000007 <- pin_site.sh (PRE), post_script.sh (POST)
|   +-- merge <- pin_site.sh (PRE)
|   +-- cleanup <- pin_site.sh (PRE)
+-- SUBDAG EXTERNAL mg_000001 -> group.dag (1 DAGMan process)
|   +-- ...
+-- SUBDAG EXTERNAL mg_000499 -> group.dag (1 DAGMan process)
    +-- ...

Total: 501 DAGMan processes, ~5000 vanilla jobs
```

**What SUBDAG EXTERNAL provides:**
1. Atomic WU retry (RETRY on outer node -> re-runs entire landing->proc->merge->cleanup)
2. wu_post.sh as POST on outer node (site exclusion between retries)
3. Isolated rescue DAG per WU
4. Clean WU boundary for monitoring (inner status file per WU)

## Options

### Option A: Flat DAG (Single DAGMan Process)

Flatten everything into one DAG with explicit PARENT/CHILD edges.
Node naming: `mg000_landing`, `mg000_proc000`, `mg000_merge`, `mg000_cleanup`.

```
workflow.dag (1 DAGMan process)
+-- mg000_landing -> mg000_proc_000..007 -> mg000_merge -> mg000_cleanup
+-- mg001_landing -> mg001_proc_000..007 -> mg001_merge -> mg001_cleanup
+-- mg499_landing -> mg499_proc_000..007 -> mg499_merge -> mg499_cleanup

Total: 1 DAGMan process, ~5000 vanilla jobs
```

**Pros:**
- 1 DAGMan process instead of 501 -- eliminates schedd pressure entirely
- Instant startup (no sub-DAG parsing)
- No orphan zombie problem
- Simpler spool (one level of files)

**Cons:**
- **No atomic WU retry.** DAGMan RETRY applies per-node, not per-group. If
  proc_003 fails, only proc_003 retries -- not the whole WU with fresh site
  election. Workaround: POST script on each proc node does site exclusion
  and memory adjustment already. The only missing piece is "retry all procs
  at a different site after infrastructure failure."
- **wu_post.sh loses its trigger point.** Currently runs as POST on the outer
  SUBDAG node. In a flat DAG, there's no single node representing the WU.
  Workaround: merge node's POST script could take over WU-level analysis.
- **Single rescue DAG** for the entire round instead of per-WU. Less granular
  recovery -- but WMS2 already handles this at the round level (replan with
  site exclusions).
- PRE/POST scripts still work per-node (site pinning, error classification).

**WU retry replacement strategy:**
The current two-level retry (inner proc RETRY + outer SUBDAG RETRY) could
be replaced by:
- proc nodes: RETRY 5 (same as now)
- If a WU's procs all exhaust retries -> merge never runs -> cleanup never
  runs -> DAGMan marks those nodes as failed -> WMS2 error handler sees
  the failed WU at round completion and includes those events in the next round
- Site exclusion: post_script.sh already records failed machines/sites.
  pin_site.sh already reads exclusion files. The mechanism works per-node.

**What we lose:** The ability to retry the entire WU at a completely different
site after the first attempt fails. Currently wu_post.sh excludes the bad
site and the outer RETRY re-runs landing at a new site. In a flat DAG, once
the landing elects a site, all procs are pinned there. If the site is bad,
individual proc retries hit the same bad site.

**Mitigation:** Make pin_site.sh smarter -- if a proc fails with infrastructure
error, the next retry's PRE script could re-elect a site (reset the elected
site file, submit a new lightweight "re-election" job). Complex but possible.

### Option B: SPLICE (Single DAGMan, Semantic Grouping)

Replace SUBDAG EXTERNAL with SPLICE. All nodes merge into one DAGMan process
but retain group naming.

```
workflow.dag (1 DAGMan process)
+-- SPLICE mg_000000 mg_000000/group.dag
+-- SPLICE mg_000001 mg_000001/group.dag
+-- SPLICE mg_000499 mg_000499/group.dag

Nodes visible as: mg_000000+landing, mg_000000+proc_000000, etc.
Total: 1 DAGMan process, ~5000 vanilla jobs
```

**Pros:**
- 1 DAGMan process -- same scaling benefit as flat DAG
- Preserves group.dag files (minimal code change to generation)
- Node naming hierarchy (mg_000000+proc_000000) aids monitoring
- PARENT/CHILD on splice boundaries works (landing is initial, cleanup is terminal)

**Cons:**
- **No atomic SPLICE retry.** Same limitation as flat DAG -- RETRY cannot
  target a SPLICE as a whole. Individual nodes retry, but not the group.
- **No PRE/POST on SPLICE boundary.** wu_post.sh cannot be a POST script
  on the splice. Same workaround as flat DAG (move to merge POST).
- **No separate rescue DAG per splice.** Single rescue for entire DAG.
- DAG files must exist at parse time (they do -- we generate them before
  submission).
- Less tested in production at CMS than SUBDAG EXTERNAL.

**Essentially the same trade-offs as flat DAG**, with slightly cleaner
code organization (group.dag files preserved).

### Option C: Fewer, Larger SUBDAGs (Batched WUs)

Keep SUBDAG EXTERNAL but group multiple WUs per sub-DAG. Instead of 500
SUBDAGs with 1 WU each, use 50 SUBDAGs with 10 WUs each (or 10 with 50).

```
workflow.dag (1 DAGMan process)
+-- SUBDAG EXTERNAL batch_00 -> batch.dag (1 DAGMan, 10 WUs)
|   +-- mg000: landing -> procs -> merge -> cleanup
|   +-- mg001: landing -> procs -> merge -> cleanup
|   +-- mg009: landing -> procs -> merge -> cleanup
+-- SUBDAG EXTERNAL batch_01 -> batch.dag (1 DAGMan, 10 WUs)
+-- SUBDAG EXTERNAL batch_49 -> batch.dag (1 DAGMan, 10 WUs)

Total: 51 DAGMan processes (not 501), ~5000 vanilla jobs
```

**Pros:**
- 10x fewer DAGMan processes (51 vs 501)
- Retains SUBDAG EXTERNAL benefits (batch-level retry, POST scripts)
- Incremental change -- same architecture, just grouped
- Each batch still gets its own rescue DAG
- No loss of any current feature

**Cons:**
- Batch-level retry retries ALL WUs in the batch (10 WUs), not just the
  failed one. Wastes work on the 9 good WUs. (Rescue DAG mitigates this --
  only failed nodes re-run.)
- wu_post.sh now covers a batch, not a single WU. Site exclusion logic
  needs to handle multiple WUs with potentially different elected sites.
- Monitoring complexity: WU completion detection needs to look inside
  batch sub-DAGs rather than at SUBDAG node completion.
- Still has the orphan problem (at smaller scale).

### Option D: Keep SUBDAG EXTERNAL, Fix Operational Issues

Don't change the architecture. Instead, fix the specific problems observed:

1. **Orphan cleanup**: When marking a DAG as failed, also `condor_rm` all
   child scheduler universe jobs (`DAGManJobId == cluster_id`).
2. **Startup time**: Accept 15 min parse time for 500 WUs. Or reduce
   `work_units_per_round` for large requests (cap at e.g. 200 WUs/round,
   more rounds).
3. **Spool size**: Already fixed (shared pileup_files.json).

**Pros:**
- Zero architecture change
- All current features preserved
- Orphan fix is a few lines of code
- WU cap is a configuration change

**Cons:**
- 501 DAGMan processes per round remains the baseline load
- Shared schedd (vocms047) hosts other users' jobs -- 500 DAGMan processes
  is not polite
- Scaling to 1000+ WUs/round remains impractical

### Option E: HTCondor Feature Requests (Fix at the Core)

The root cause is that HTCondor offers no mechanism for grouping nodes into
retryable units within a single DAGMan process. SUBDAG EXTERNAL provides
the semantics we need but forces a separate process per group. SPLICE
provides single-process operation but lacks group-level retry/POST.

Two feature requests that would eliminate the problem:

#### E1: SPLICE with RETRY and POST scripts

Allow `RETRY` and `SCRIPT POST` on SPLICE boundaries, same as SUBDAG
EXTERNAL but without spawning a separate DAGMan process.

```
SPLICE mg_000000 mg_000000/group.dag
SCRIPT POST mg_000000 wu_post.sh $JOB $RETURN mg_000000
RETRY mg_000000 1 UNLESS-EXIT 42
```

**What this gives us:**
- Single DAGMan process (SPLICE semantics)
- Atomic group retry (currently SUBDAG-only)
- POST script on group completion (currently SUBDAG-only)
- Rescue DAG could track per-splice progress

**HTCondor implementation complexity:** Medium. SPLICE already tracks
initial/terminal nodes of each splice. Adding RETRY would re-queue all
splice nodes on failure. POST would fire after all terminal nodes complete.
The node naming hierarchy (`mg_000000+proc_000000`) already exists.

**This is the ideal solution.** It makes Options A-D unnecessary. WMS2
would change one line: `SUBDAG EXTERNAL` -> `SPLICE` in the outer DAG,
and everything else stays the same.

#### E2: Lightweight SUBDAG (in-process sub-DAGs)

Instead of spawning `condor_dagman` per SUBDAG EXTERNAL, manage sub-DAGs
as internal node groups within the parent DAGMan process. Same semantics
(RETRY, POST, rescue), single process.

**What this gives us:** Identical to current behavior, zero WMS2 code changes.

**HTCondor implementation complexity:** High. Requires refactoring DAGMan's
internal node management to support nested scopes without separate processes.
Essentially making SUBDAG EXTERNAL behave like SPLICE under the hood while
preserving its full API.

#### Assessment

E1 (SPLICE + RETRY) is the more realistic request:
- Well-scoped: extends existing SPLICE with two features (RETRY, POST)
- SPLICE infrastructure already handles node grouping and naming
- The HTCondor team has acknowledged this gap (SPLICE docs say "use SUBDAG
  EXTERNAL if you need retry")
- Could be implemented incrementally (POST first, then RETRY)

E2 is a larger architectural change inside HTCondor with the same end result
for users but higher implementation cost.

## Comparison Matrix

| Criterion | A: Flat | B: SPLICE | C: Batched | D: Status Quo |
|-----------|---------|-----------|------------|---------------|
| DAGMan processes | 1 | 1 | ~50 | ~500 |
| Schedd load | Minimal | Minimal | Moderate | Heavy |
| Atomic WU retry | No | No | Batch-level | Yes |
| wu_post.sh boundary | Lost | Lost | Batch-level | Per-WU |
| Rescue granularity | Round | Round | Batch | Per-WU |
| Code change size | Large | Medium | Medium | Small |
| Risk | High | Medium | Low | None |
| Startup time | Instant | Instant | ~2 min | ~15 min |

## Recommendation

**Immediate (Option D):** Fix operational issues -- orphan cleanup on DAG
failure, cap `work_units_per_round`. Zero architecture change, unblocks
current operations.

**Primary strategy (Option E1):** File HTCondor feature request for SPLICE
with RETRY + POST support. This is the clean solution that eliminates the
problem at the source.

**Fallback (Option C):** If HTCondor can't add SPLICE retry in a useful
timeframe, batch WUs into larger SUBDAGs (10-20 WUs/batch). Reduces
DAGMan count 10-20x with modest code changes and acceptable trade-offs
on retry granularity.

**Not recommended for now:** Options A and B sacrifice atomic WU retry
without an HTCondor-level replacement. The retry mechanism has proven
valuable in production (site exclusion + re-election at a different site
saved multiple WUs during 00057 processing). Giving it up should only
happen if HTCondor provides an equivalent.
