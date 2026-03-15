# Condora Testing Specification

**OpenSpec v1.0**

| Field | Value |
|---|---|
| **Spec Version** | 2.0.0 |
| **Status** | DRAFT |
| **Date** | 2026-03-15 |
| **Authors** | CMS Computing |
| **Parent Spec** | docs/spec.md (Condora v2.7.0) |

---

## 1. Overview

### 1.1 Purpose

This document specifies the Condora test infrastructure, strategy, and verification model. It is the source of truth for how Condora is tested — from isolated unit tests through full end-to-end workflow execution on a live HTCondor pool.

### 1.2 Relationship to Main Spec

Testing validates the design described in `docs/spec.md`. The main spec defines *what* Condora does; this document defines *how we verify it does so correctly*. Cross-references to main spec sections use the notation `§N` (e.g., `§4.5` for the DAG Planner).

### 1.3 Design Principle: Production Code Reuse

The test matrix calls the same production code paths used by Condora itself:

- `condora.core.sandbox.create_sandbox()` for sandbox building
- `condora.core.dag_planner.DAGPlanner.plan_production_dag()` for DAG planning and submit file generation
- `condora.adapters.condor.HTCondorAdapter` for real HTCondor operations
- `condora.core.dag_planner.PilotMetrics.from_request()` for resource estimates

This ensures tests exercise actual production logic, not test-specific reimplementations. Only external service adapters (DBS, Rucio, ReqMgr2) are mocked at the boundary.

---

## 2. Testing Strategy

### 2.1 Principles

Condora testing is organized around two axes: **what is tested** (scope) and **when tests run** (trigger). The goal is fast feedback on common changes and thorough validation before major milestones.

Three principles guide the strategy:

1. **Faster tests run more often.** A 15-second unit test suite runs on every commit. A 2-hour CMSSW validation runs before commissioning a new request type. Each tier catches a different class of bug; skipping a tier means accepting the risk of that class going undetected until later.

2. **Production code reuse.** Tests call the same code paths as the production system (§1.3). Only external service boundaries (DBS, Rucio, ReqMgr2) are mocked. This eliminates the class of bugs where tests pass against a reimplementation but fail against real code.

3. **Real data over synthetic mocks.** Where practical, tests use artifacts captured from real workflow executions rather than hand-crafted mocks. Mocks diverge from reality over time; captured data reflects actual system behavior.

### 2.2 Test Tiers

Condora organizes all testing into six tiers, ordered by execution time:

| Tier | Name | What runs | Runtime | Infrastructure |
|---|---|---|---|---|
| **T0** | Lint | `ruff check`, `mypy` | 2–5 sec | Python only |
| **T1** | Unit | `pytest tests/unit/` | ~15 sec | Python only |
| **T2a** | Component | `pytest tests/component/` | 30–60 sec | Python + test data on disk |
| **T2b** | Integration | `pytest tests/integration/` | ~1 min | Python + PostgreSQL |
| **T3** | Smoke E2E | `python -m tests.matrix -l smoke` | ~5 min | Python + HTCondor local pool |
| **T4** | Full E2E | `python -m tests.matrix -l full` | 30–120 min | Full stack (HTCondor, CVMFS, apptainer) |

Each tier catches a distinct class of bug:

| Tier | What it catches | Example |
|---|---|---|
| **T0** | Syntax errors, dead imports, type mismatches, invalid escape sequences | `\s` in a Python string instead of `\\s` — silent now, error in future Python |
| **T1** | Logic errors in state machines, wrong calculations, broken parsing, invalid enum transitions | Lifecycle manager allows COMPLETED → ACTIVE transition (should be illegal) |
| **T2a** | Code that works with mocks but fails with real data — wrong JSON keys, unexpected FJR fields, missing file format handling | Merge script assumes `output_info.json` has a `site` key that was renamed to `cms_site` |
| **T2b** | SQL schema mismatches, API endpoint regressions, repository query bugs, transaction isolation issues | `get_active_requests()` returns stale data because the query doesn't filter by the new `paused` status |
| **T3** | DAG structure errors, merge/cleanup chain breakage, fault recovery failures, submit file generation bugs | DAGMan aborts because a SUBDAG EXTERNAL path is relative instead of absolute |
| **T4** | Adaptive algorithm regressions, real CMSSW incompatibilities, performance regressions, site-specific failures | Adaptive optimizer oscillates instead of converging when step 3 has serial_fraction > 0.8 |

### 2.3 When to Run Each Tier

```
Developer edits code
  │
  ├─ T0: Lint ─────────────── always, every commit           [5 sec]
  ├─ T1: Unit ─────────────── always, every commit           [15 sec]
  │
  ├─ T2a: Component ────────── after merge/cleanup/adaptive   [30-60 sec]
  ├─ T2b: Integration ──────── after DB/API changes           [1 min]
  │
  ├─ T3: Smoke E2E ─────────── after DAG planner, scripts,    [5 min]
  │                             or error handler changes
  │
  └─ T4: Full E2E ──────────── before commissioning new       [1-2 hours]
                                request types or after
                                major refactors
```

The trigger rules reflect where bugs are most likely to hide:

- **DAG planner changes** need T3 because the planner generates shell scripts and DAGMan files that can only be validated by real execution — unit tests cannot meaningfully verify embedded bash.
- **Adaptive optimizer changes** need T2a (component tests with real metrics) because the math is complex and unit tests with synthetic inputs may miss edge cases present in real data.
- **API/DB changes** need T2b because SQL and HTTP behavior often differs subtly from what mocks simulate.
- **Everything** needs T0+T1 because these are fast enough to never skip.

### 2.4 Mandatory Gates

| Gate | Required tiers | Enforced by |
|---|---|---|
| Every commit | T0 + T1 | Pre-commit hook (local) or CI |
| Before merging a feature | T0 + T1 + T2b | CI |
| After DAG planner or error handler changes | T0 + T1 + T3 | Developer (manual) |
| Before commissioning new request type | T0 + T1 + T2a + T2b + T3 + T4 | Developer (manual) |

**Non-negotiable rule**: T0 and T1 must pass with zero failures. A failing test suite that is routinely ignored provides negative value — it trains developers to disregard test results. If a test fails because production code changed, the test must be fixed or deleted in the same commit.

---

## 3. Test Levels

Condora uses four test levels, each with different scope, speed, and infrastructure requirements.

| Level | Scope | Speed | Infrastructure | Location |
|---|---|---|---|---|
| Unit | Individual functions/classes | Seconds | Python only | `tests/unit/` (18 files, ~5.3K lines) |
| Integration | Cross-component with real DB/services | Seconds–minutes | PostgreSQL, optionally HTCondor | `tests/integration/` (6 files, ~1.6K lines) |
| Matrix | End-to-end workflows through HTCondor | Minutes | HTCondor local pool | `tests/matrix/` (~5.3K lines) |
| Environment | Capability verification | Seconds | Varies by level | `tests/environment/` (7 files, ~400 lines) |

### 3.1 Unit Tests

Unit tests verify individual components in isolation. External dependencies (HTCondor, PostgreSQL, DBS, Rucio) are replaced with mocks from `tests/unit/conftest.py`. These tests run in seconds with no infrastructure beyond Python.

**What they catch**: Logic errors in state machines, incorrect DAG file generation, broken parsing, invalid data model validation, wrong resource calculations.

**How to run**: `pytest tests/unit/`

### 3.2 Integration Tests

Integration tests verify cross-component interactions with real services. Most require a running PostgreSQL instance; some (`test_condor_submit.py`, `test_resource_utilization.py`) require a running HTCondor pool and are gated by `@pytest.mark.level2`.

**What they catch**: SQL schema mismatches, API endpoint regressions, DAG planner end-to-end failures, repository CRUD bugs.

**How to run**: `pytest tests/integration/`

### 3.3 Matrix Tests

Matrix tests execute complete workflows through the full production pipeline: sandbox creation, DAG planning, HTCondor submission, monitoring, verification, and reporting. This is the primary end-to-end validation mechanism.

**What they catch**: Integration failures between Condora and HTCondor DAGMan, sandbox packaging errors, merge/cleanup chain breakage, adaptive algorithm regressions, fault recovery failures.

**How to run**: `python -m tests.matrix -l smoke` (see §5.4 for full CLI usage)

### 3.4 Environment Tests

Environment tests verify that infrastructure prerequisites are available before running higher-level tests. They are organized by capability level and used by both the test suite and `conftest.py` to gate tests.

**What they catch**: Missing services, expired credentials, unavailable CVMFS, broken HTCondor pool.

**How to run**: `pytest tests/environment/`

---

## 4. Component Testing with Captured Data

### 4.1 Motivation

Unit tests (T1) are fast but use mocks that may not match real data formats. End-to-end tests (T3/T4) use real data but take minutes to hours. Component testing fills the gap: it tests individual pipeline stages against real artifacts captured from previous E2E runs, without re-running the full pipeline.

This matters because Condora's components exchange data via files at well-defined stage boundaries. The merge script reads `output_info.json` and ROOT files produced by proc jobs. The adaptive optimizer reads `work_unit_metrics.json` produced by the DAG monitor. The stageout utility reads `storage.json` from the site configuration. When these file formats change — a key is renamed, a field is added, a nested structure is flattened — unit tests with hand-crafted mocks won't catch the mismatch. Component tests with captured real data will.

### 4.2 Pipeline Stage Boundaries

The Condora pipeline has natural boundaries where one component's output becomes another's input. These are the capture points for test data:

```
DAG Planner
  │
  ├── .dag files, .sub files, scripts, manifests
  │
  ▼
HTCondor runs proc jobs
  │
  ├── ROOT files, FJR XML, proc_*_metrics.json, output_info.json
  │
  ▼
Merge script
  │
  ├── merged ROOT files, merge_output.json
  │
  ▼
Cleanup script
  │
  ├── cleanup_manifest.json (consumed)
  │
  ▼
DAG Monitor (WU completion)
  │
  ├── work_unit_metrics.json
  │
  ▼
Adaptive optimizer (round completion)
  │
  ├── round metrics, manifest_tuned.json
  │
  ▼
DAG Planner (next round)
```

Each arrow is a testable interface. A component test loads saved artifacts from the upstream side and runs the downstream component against them.

| Boundary | Files captured | Downstream component tested |
|---|---|---|
| After proc jobs | ROOT files, FJR XML, proc_*_metrics.json, output_info.json | Merge script, stageout utility |
| After merge | merge_output.json, merged ROOT files | Cleanup script, Output Manager |
| After WU completion | work_unit_metrics.json, enriched WU data | Adaptive optimizer |
| After round completion | Round metrics, per-WU summaries | Round planner, adaptive convergence |
| After DAG planning | .dag, .sub, scripts, manifests | Submit file validation (no HTCondor needed) |
| Site configuration | storage.json (prefix, rules, chain formats) | Stageout utility LFN→PFN resolution |

### 4.3 Test Data Catalog

Test data is stored outside the git repository (artifacts are too large — ROOT files are hundreds of MB each) in a managed directory:

```
{test_data_root}/
  catalog.json
  partial/
    {label}/
      README.md                      # source request, round, date, commit
      output_info.json
      proc_000000_metrics.json
      proc_000001_metrics.json
      proc_000000_AODSIM.root
      proc_000001_AODSIM.root
      manifest.json
      work_unit_metrics.json
      merge_output.json
      storage.json
  full/
    {label}/
      README.md
      mg_000000/
        (same structure as partial)
      mg_000001/
      ...
      dag_metrics.json
      round_summary.json
```

The default `test_data_root` is configured via `CONDORA_TEST_DATA_ROOT` environment variable (default: `/mnt/shared/work/condora_test_data/`).

`catalog.json` indexes available datasets:

```json
{
  "datasets": [
    {
      "label": "aodsim_2proc_00058_r3",
      "scale": "partial",
      "source_request": "GEN-00058",
      "source_round": 3,
      "merge_group": "mg_000000",
      "captured_at": "2026-03-15T14:30:00Z",
      "code_commit": "c2de1a2",
      "steps": ["GEN-SIM", "DIGI", "RECO", "MINIAODSIM", "NANOAODSIM"],
      "stageout_mode": "grid",
      "site": "T2_CH_CERN"
    }
  ]
}
```

### 4.4 Data Scales

Two scales serve different testing needs:

| Scale | Contents | Size | What it tests |
|---|---|---|---|
| **Partial** | One work unit's artifacts: proc outputs, metrics, merge inputs/outputs, cleanup manifest | 1–5 GB | Merge script, cleanup script, stageout, FJR parsing, per-WU adaptive optimization |
| **Full** | One complete round: all work units, DAG-level metrics, round summary | 10–50 GB | Round completion logic, cross-WU metric aggregation, round-to-round adaptive optimization, output manager block assembly |

Partial datasets are the primary tool — they cover the most common component tests. Full datasets are needed only for testing round-level and cross-WU logic.

### 4.5 Capturing Test Data

Test data is captured as a byproduct of T4 (full E2E) runs. A capture tool copies the relevant stage-boundary files from the work directory into the catalog:

```bash
# Capture one WU's data (partial scale)
python -m tests.capture --request GEN-00058 --round 3 --merge-group mg_000000 --scale partial

# Capture an entire round (full scale)
python -m tests.capture --request GEN-00058 --round 3 --scale full

# List available datasets
python -m tests.capture --list
```

The capture tool:
1. Reads the request's work directory (`submit_base_dir/{request_name}/`)
2. Identifies stage-boundary files for the specified round/merge-group
3. Copies them into `{test_data_root}/{scale}/{label}/`
4. Writes a `README.md` with provenance (request, round, date, commit hash)
5. Updates `catalog.json`

### 4.6 Staleness and Versioning

Captured data can become invalid when artifact formats change (e.g., enriched WU metrics changed from string list to dict list). To manage this:

- `catalog.json` records the git commit hash that produced each dataset
- Component tests log a warning when data is older than 30 days
- When a format-breaking change is made, the relevant datasets must be recaptured from a new T4 run
- Old datasets are not automatically deleted — they remain until manually pruned

### 4.7 Running Component Tests

```bash
# Run all component tests (skips if test data not present)
pytest tests/component/

# Run only merge-related component tests
pytest tests/component/test_merge.py

# Run with verbose dataset info
pytest tests/component/ -v
```

Component tests use a `@pytest.mark.test_data` marker that specifies the required dataset. The test is automatically skipped with a clear message if the dataset is not available:

```python
@pytest.mark.test_data("partial/aodsim_2proc_00058_r3")
def test_merge_produces_correct_output(test_data_dir):
    """Merge 2 proc AODSIM outputs and verify merged ROOT file."""
    output_info = json.load(open(test_data_dir / "output_info.json"))
    # ... run merge logic against real files ...
    assert merged_file.exists()
    assert get_root_entries(merged_file) == expected_events
```

On a machine where T4 has not been run (CI, fresh clone), all component tests skip gracefully. On the dev VM where test data has been captured, they execute and provide fast validation.

### 4.8 Components with Highest Value from Component Testing

Listed by priority based on bug history and complexity:

| Component | Why | Key test scenarios |
|---|---|---|
| Merge script | Most production bugs (issues #23, #24, #25, #26) | Multi-proc merge, oversized file skip, tier-specific merging, grid storage URL resolution |
| Adaptive optimizer | Complex math, documented bugs | Per-step nThreads from real metrics, job split under memory constraints, round-over-round convergence |
| Stageout utility | Three storage.json formats, site-specific | LFN→PFN for prefix (CERN), rules (FNAL), chain (KIT) formats |
| POST script classifier | 34 error patterns, commissioning-critical | Real exit codes and job ads from production failures |
| FJR parser | Multiple CMSSW versions, field variations | Real FJR XML from different step types (GEN, DIGI, RECO, NANO) |

---

## 5. Test Matrix System

The test matrix is the core end-to-end testing infrastructure. It submits real workflows to a local HTCondor pool and verifies the complete output pipeline.

### 5.1 Architecture

The matrix system consists of these modules:

| Module | Role |
|---|---|
| `runner.py` | Orchestration engine: sandbox creation, DAG planning, HTCondor submission, monitoring, verification, reporting |
| `catalog.py` | Workflow definitions — the single source of numbered `WorkflowDef` entries |
| `definitions.py` | Data structures: `WorkflowDef`, `FaultSpec`, `VerifySpec` |
| `sets.py` | Named workflow groupings (smoke, integration, synthetic, simulator, faults, adaptive, full) |
| `reporter.py` | Pass/fail tables, per-step performance metrics, throughput comparison, CPU timeline plots |
| `sweeper.py` | Pre/post cleanup — removes work directories, LFN output paths, DAGMan jobs |
| `environment.py` | Capability detection — wraps `tests/environment/checks.py` for workflow prerequisite gating |
| `__main__.py` | CLI entry point for `python -m tests.matrix` |

#### Execution Flow

```
┌─────────────────────────────────────────────────────┐
│  MatrixRunner.run_many(workflows)                   │
│                                                     │
│  for each workflow:                                 │
│    1. sweep_pre()         — clean work dir + LFNs   │
│    2. create_sandbox()    — build sandbox tarball    │
│    3. plan_production_dag() — generate .dag + .sub   │
│    4. inject_faults()     — if fault spec present    │
│    5. condor_submit_dag   — submit to HTCondor       │
│    6. poll DAGMan status  — until done or timeout    │
│    7. verify outcomes     — check VerifySpec         │
│    8. collect perf data   — parse FJR/metrics        │
│    9. sweep_post()        — cleanup on success       │
│                                                     │
│  save_results() → print_summary()                   │
└─────────────────────────────────────────────────────┘
```

For adaptive workflows, steps 2–8 repeat per work unit with a replan phase between rounds (see §7).

### 5.2 Workflow Catalog

Workflows are numbered with an ID scheme that encodes the execution mode and purpose:

| Range | Mode | Purpose |
|---|---|---|
| 100.x | Synthetic | Fast baseline — no CMSSW, tests DAG plumbing |
| 150.x | Simulator | Real ROOT/FJR artifacts via cmsRun simulator, no CMSSW |
| 300.x | Real CMSSW | Production-like StepChain execution via cached sandbox |
| 350.x | Adaptive | Adaptive nThreads tuning (per-step split, overcommit, pipeline) |
| 360.x | Adaptive | Overcommit-only mode |
| 370.x | Adaptive | Split + overcommit combined |
| 380.x | Adaptive | All-step pipeline split |
| 391.x–392.x | Adaptive | Job split (more jobs, fewer cores per job in Round 2) |
| 500.x | Fault injection | Process exits, SIGKILL, merge failures |

The variant convention is: `x00.0` = plumbing (1–2 jobs), `x00.1` = performance (4–8 jobs).

#### Full Catalog

| ID | Title | Mode | Jobs | WUs | Cores | Requires | Purpose |
|---|---|---|---|---|---|---|---|
| 100.0 | Synthetic baseline, 1 job | synthetic | 1 | 1 | 1 | condor | DAG plumbing smoke test |
| 100.1 | Synthetic baseline, 8 jobs | synthetic | 8 | 1 | 1 | condor | Multi-job merge chain |
| 150.0 | Simulator single-step, 1 job | simulator | 1 | 1 | 4 | condor | Simulator plumbing |
| 150.1 | Simulator single-step, 4 jobs | simulator | 4 | 1 | 4 | condor | Multi-job simulator |
| 151.0 | Simulator 3-step StepChain, 2 jobs | simulator | 2 | 1 | 8 | condor | Multi-step simulator |
| 152.0 | Simulator high-memory profile | simulator | 2 | 1 | 8 | condor | High-memory resource model |
| 155.0 | Simulator adaptive 2-WU probe split | simulator | 2 | 2 | 8 | condor | Adaptive probe with simulator |
| 155.1 | Simulator adaptive job split | simulator | 2 | 2 | 8 | condor | Job split with simulator |
| 300.0 | NPS 5-step StepChain, 1 WU | cached | 2 | 1 | 8 | condor, cvmfs, siteconf, apptainer | Real CMSSW baseline |
| 300.1 | NPS 5-step StepChain, 16-core | cached | 4 | 1 | 16 | condor, cvmfs, siteconf, apptainer | High-core count |
| 301.0 | DY2L 5-step StepChain, 8 jobs | cached | 8 | 1 | 8 | condor, cvmfs, siteconf, apptainer | Large real CMSSW |
| 350.0 | Adaptive nThreads tuning, 40 ev/job | cached | 2 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Basic adaptive |
| 350.1 | Adaptive nThreads tuning, 10 ev/job | cached | 8 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Fast adaptive iteration |
| 351.0 | DY2L adaptive step 1 split, 2 jobs | cached | 2 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Per-step split with probe |
| 351.1 | DY2L adaptive step 1 split, 4 jobs | cached | 4 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Per-step split at scale |
| 360.0 | Adaptive overcommit only | cached | 8 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Overcommit without split |
| 370.0 | Adaptive split + overcommit (1.25x) | cached | 8 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Combined modes |
| 380.0 | Adaptive all-step pipeline split | cached | 8 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Pipeline parallelism |
| 380.1 | Pipeline split, uniform threads | cached | 8 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Uniform nThreads pipeline |
| 391.0 | DY2L adaptive job split, 2 jobs | cached | 2 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Job split (small) |
| 391.1 | DY2L adaptive job split, 4 jobs | cached | 4 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Job split (medium) |
| 391.2 | DY2L adaptive job split, 3 GB/core | cached | 4 | 2 | 8 | condor, cvmfs, siteconf, apptainer | Job split high memory |
| 392.0 | DY2L adaptive job split 3-round | cached | 2 | 3 | 8 | condor, cvmfs, siteconf, apptainer | 3-round convergence |
| 500.0 | Fault: proc exits 1 | synthetic | 1 | 1 | 1 | condor | Retry exhaustion → rescue DAG |
| 501.0 | Fault: proc SIGKILL | synthetic | 2 | 1 | 1 | condor | Signal-based failure |
| 510.0 | Fault: merge exits 1 | synthetic | 2 | 1 | 1 | condor | Merge failure → rescue DAG |

### 5.3 Workflow Sets

Named sets group workflows for different testing scenarios:

| Set | Workflows | Purpose | When to Use |
|---|---|---|---|
| `smoke` | 100.0, 150.0, 500.0, 501.0, 510.0 | Quick sanity check | After any code change; CI gate |
| `synthetic` | All `sandbox_mode="synthetic"` | DAG plumbing without CMSSW | Testing DAG structure changes |
| `simulator` | All `sandbox_mode="simulator"` | Full pipeline with simulated physics | Testing output pipeline, metrics |
| `faults` | All workflows with `fault != None` | Recovery path validation | After error handler changes |
| `adaptive` | All workflows with `adaptive=True` | Adaptive algorithm validation | After adaptive/replan changes |
| `integration` | All except scale-only variants | Comprehensive validation | Pre-merge validation |
| `full` | All catalog entries | Complete regression | Release validation |

### 5.4 Running the Matrix

CLI entry point: `python -m tests.matrix`

```
# Run a named set
python -m tests.matrix -l smoke
python -m tests.matrix -l adaptive

# Run specific workflow IDs
python -m tests.matrix -l 100.0,500.0

# Run an ID range
python -m tests.matrix -l 500-530

# List workflows and environment status
python -m tests.matrix --list
python -m tests.matrix --list -l integration

# List available sets
python -m tests.matrix --sets

# Re-render report from saved results
python -m tests.matrix --report
python -m tests.matrix --report /path/to/results.json

# List saved result files
python -m tests.matrix --results

# Enable debug logging
python -m tests.matrix -l smoke -v
```

Results are saved to `/mnt/shared/work/condora_matrix/results/` and can be re-rendered at any time with `--report`.

---

## 6. Execution Modes

Three sandbox/payload modes provide different fidelity levels. Each uses the same DAG planning and submission infrastructure — only the payload inside each job differs.

### 6.1 Synthetic Mode

**Sandbox mode**: `synthetic`

Synthetic mode creates trivially sized output files without any real processing. The executable is a simple shell script that touches files in the expected locations.

- **Tests**: DAG structure, merge chain correctness, cleanup execution, stage-out paths
- **Speed**: Seconds per workflow (dominated by HTCondor scheduling overhead)
- **Infrastructure**: HTCondor only
- **Use case**: DAG plumbing validation, fault injection targets

### 6.2 Simulator Mode

**Sandbox mode**: `simulator`

The cmsRun simulator (`src/condora/core/simulator.py`) replaces real CMSSW with a lightweight Python script that produces identical artifacts in seconds:

- **ROOT files**: Valid ROOT files with `Events` TTree via `uproot`, sized to match expected output
- **FJR XML**: Framework Job Report XML compatible with all Condora parsers (timing, memory, throughput, storage metrics)
- **Memory simulation**: Allocates real memory via `bytearray` with page-touching for accurate HTCondor `ImageSize` reporting
- **CPU simulation**: Threaded busy loops matching the Amdahl's law resource profile for accurate `cpu.stat` accounting
- **Step chaining**: Output of step N is input to step N+1, matching real StepChain behavior

#### Resource Models

The simulator uses physics-based resource profiles per step type:

**CPU efficiency** — Amdahl's law:
```
eff_cores = serial_fraction + (1 - serial_fraction) * ncpus
cpu_efficiency = eff_cores / ncpus
```

**Memory** — linear model:
```
peak_rss_mb = base_memory_mb + marginal_per_thread_mb * ncpus
```

**Wall time**:
```
wall_sec = events * wall_sec_per_event / eff_cores
```

Each step type (GEN-SIM, DIGI, RECO, NANO) has its own `serial_fraction`, `base_memory_mb`, `marginal_per_thread_mb`, and `wall_sec_per_event` parameters configured in the manifest.

- **Tests**: Full output pipeline, metrics extraction, adaptive algorithm feedback loops
- **Speed**: Seconds per job (wall time dominated by HTCondor scheduling + simulated processing)
- **Infrastructure**: HTCondor only (no CVMFS, no apptainer)
- **Use case**: Rapid adaptive algorithm iteration, output pipeline validation

### 6.3 Real CMSSW Mode

**Sandbox mode**: `cached`

Real CMSSW execution via pre-built sandbox tarballs. Jobs run actual `cmsRun` inside apptainer containers with CMSSW from CVMFS.

- **Tests**: Real physics processing, site-local configuration, proxy authentication, actual resource consumption
- **Speed**: Minutes to hours per workflow depending on step count and event count
- **Infrastructure**: HTCondor, CVMFS, apptainer, siteconf, optionally x509 proxy
- **Use case**: Production-fidelity validation, adaptive algorithm calibration against real workloads

---

## 7. Adaptive Execution Testing

The adaptive testing infrastructure (`tests/matrix/adaptive.py`) validates the adaptive execution model described in main spec §5. It demonstrates that Condora can converge resource estimates from initial guesses to measured values within a small number of work unit rounds.

### 7.1 Two-Round Execution Model

The basic adaptive pattern uses two work units (WU0 and WU1):

```
┌──────────────────────────────────────────────┐
│  Round 1 (WU0): Baseline execution           │
│    - Execute at nominal nThreads             │
│    - Collect work_unit_metrics.json           │
│                                              │
│  Replan:                                     │
│    - Read WU0 metrics                        │
│    - Compute per-step optimal nThreads       │
│    - Write manifest_tuned.json for WU1       │
│                                              │
│  Round 2 (WU1): Tuned execution              │
│    - Execute with optimized parameters       │
│    - Collect metrics for comparison           │
│                                              │
│  Verify:                                     │
│    - Compare R1 vs R2 efficiency             │
│    - Check convergence criterion             │
└──────────────────────────────────────────────┘
```

The `analyze_wu_metrics()` function reads `proc_*_metrics.json` files from a completed merge group and aggregates per-step metrics (wall time, CPU efficiency, peak RSS). The `compute_per_step_nthreads()` function derives optimal nThreads for each step from observed CPU efficiency. The `patch_wu_manifests()` function writes `manifest_tuned.json` and updates proc submit files.

### 7.2 Adaptive Modes

The catalog covers several adaptive strategies:

| Mode | Workflows | Description |
|---|---|---|
| Per-step nThreads tuning | 350.x | Reduce nThreads for low-efficiency steps |
| Step 0 parallel split | 351.x | Run step 0 as N parallel instances at reduced threads |
| Overcommit | 360.0 | Allow CPU overcommit up to a configured ratio |
| Split + overcommit | 370.0 | Combine parallel split with overcommit |
| Pipeline split | 380.x | Split all steps into concurrent pipeline stages |
| Job split | 391.x, 392.x | More jobs with fewer cores per job in Round 2 |

### 7.3 Probe Split Testing

Probe split workflows (those with `probe_split=True`) run one WU0 processing job as 2 instances at half-threads. This provides:

- **cgroup memory measurement**: Accurate per-instance memory from HTCondor cgroup accounting
- **R2 sizing input**: Measured memory informs `manifest_tuned.json` for Round 2
- **Split feasibility**: Determines whether parallel splitting is memory-safe

### 7.4 Multi-Round Convergence

Workflow 392.0 tests 3-round convergence (`num_work_units=3`), validating that the adaptive algorithm stabilizes rather than oscillating. The reporter dynamically discovers rounds from step name prefixes and shows pairwise throughput comparison.

### 7.5 Simulator + Adaptive Integration

The simulator responds to `ncpus` changes via Amdahl's law, enabling deterministic adaptive testing:

- Known `serial_fraction` per step produces predictable efficiency curves
- Simulator 155.0 tests the full adaptive loop without CMSSW
- Enables rapid iteration on adaptive algorithm changes

The success criterion from main spec §13: *"resource estimates within 20% of actual by round 2"*.

---

## 8. Fault Injection

The fault injection system (`tests/matrix/faults.py`) validates Condora's recovery paths by introducing controlled failures into DAG execution.

### 8.1 Injection Mechanism

Faults are injected **after** DAG file generation but **before** HTCondor submission. The `inject_faults()` function patches `.sub` files to replace the real executable with a fault wrapper script. DAGMan reads `.sub` files at node start time (not submission time), so patches applied after `condor_submit_dag` are picked up correctly.

### 8.2 Fault Types

Faults are specified via `FaultSpec`:

```python
@dataclass(frozen=True)
class FaultSpec:
    target: str          # "proc" | "merge" | "cleanup"
    node_indices: tuple[int, ...] | None = None  # None = all nodes of that type
    exit_code: int = 0   # Non-zero exit code
    signal: int = 0      # Signal number (e.g., 9 for SIGKILL)
    delay_sec: int = 0   # Delay before fault
    skip_output: bool = False
```

| Fault | Workflow | Target | Effect | Expected Recovery |
|---|---|---|---|---|
| Process exit 1 | 500.0 | proc | All proc nodes exit with code 1 | DAGMan retries exhaust → rescue DAG |
| Process SIGKILL | 501.0 | proc | All proc nodes killed by signal 9 | Rescue DAG |
| Merge exit 1 | 510.0 | merge | Merge node exits with code 1 | Rescue DAG |

### 8.3 Verification

Each fault workflow has a `VerifySpec` that declares expected outcomes:

```python
@dataclass(frozen=True)
class VerifySpec:
    expect_success: bool = True
    expect_rescue_dag: bool = False
    expect_merged_outputs: bool = True
    expect_cleanup_ran: bool = True
    expect_dag_status: str | None = None
```

For fault workflows, `expect_success=False` and `expect_rescue_dag=True` — the test passes when DAGMan correctly produces a rescue DAG rather than reporting success.

---

## 9. Verification Model

The matrix runner verifies each workflow against its `VerifySpec` after execution completes. Verification covers:

### 9.1 DAG Status

- **Success**: DAGMan reports completed status
- **Failure**: DAGMan reports failed status (expected for fault workflows)
- Checked against `VerifySpec.expect_success` and `VerifySpec.expect_dag_status`

### 9.2 Rescue DAG

- Presence or absence of rescue DAG file in the submit directory
- Expected for fault workflows (`expect_rescue_dag=True`)
- Absence expected for normal workflows

### 9.3 Output Verification

- **Merged outputs**: Merged output files exist at expected LFN paths (`expect_merged_outputs`)
- **Cleanup execution**: Unmerged intermediate files have been removed (`expect_cleanup_ran`)

### 9.4 Performance Metrics

For passing workflows, the reporter collects:

- **Per-step metrics**: Wall time, CPU efficiency, peak RSS, event throughput
- **Throughput**: Events per core-hour computed from actual wall time and core allocation
- **Time breakdown**: Processing, merge, cleanup, and overhead phases from DAGMan log
- **CPU timeline**: Sampled CPU usage over time (cgroup-based or `/proc/stat` fallback)
- **Adaptive comparison**: Round-over-round throughput and efficiency deltas

### 9.5 Adaptive Convergence

For adaptive workflows, the reporter compares consecutive rounds:

- **Events per core-hour**: Should improve from Round 1 to Round 2
- **CPU efficiency per job**: Should increase as nThreads are tuned to match actual parallelism
- **Memory sizing**: R2 `request_memory` should reflect measured RSS from R1

---

## 10. Unit Test Coverage

Unit tests (`tests/unit/`) cover all core Condora components using mock fixtures defined in `conftest.py`.

### 10.1 Mock Strategy

`tests/unit/conftest.py` provides pytest fixtures that replace external dependencies:

| Fixture | Replaces | Strategy |
|---|---|---|
| `mock_repository` | `Repository` (PostgreSQL) | `MagicMock(spec=Repository)` with `AsyncMock` returns |
| `mock_reqmgr` | `MockReqMgrAdapter` | In-memory adapter from `condora.adapters.mock` |
| `mock_dbs` | `MockDBSAdapter` | In-memory adapter |
| `mock_rucio` | `MockRucioAdapter` | In-memory adapter |
| `mock_condor` | `MockCondorAdapter` | In-memory adapter |
| `settings` | `Settings` | Test configuration with short timeouts |

The `make_request_row()` helper creates mock request database rows with sensible defaults.

### 10.2 Test File Map

| File | Component (spec §) | What It Tests |
|---|---|---|
| `test_lifecycle.py` | Lifecycle Manager (§4.2) | Request state machine transitions, timeout handling, lifecycle handlers |
| `test_workflow_manager.py` | Workflow Manager (§4.3) | Workflow import, creation, request-to-workflow conversion |
| `test_admission.py` | Admission Controller (§4.4) | Capacity checking, priority ordering |
| `test_dag_generator.py` | DAG Planner (§4.5) | DAG file generation, SUBDAG EXTERNAL syntax, merge group structure |
| `test_merge_groups.py` | DAG Planner (§4.5) | Merge group planning with fixed job count allocations |
| `test_splitters.py` | DAG Planner (§4.5) | Event-based and file-based job splitting |
| `test_dag_monitor.py` | DAG Monitor (§4.6) | DAG status polling, node status parsing, ClassAd parsing |
| `test_output_manager.py` | Output Manager (§4.7) | ProcessingBlock lifecycle, DBS/Rucio integration |
| `test_error_handler.py` | Error Handler (§4.8) | Error classification, rescue DAG submission, abort logic |
| `test_condor_adapter.py` | HTCondor Adapter | htcondor2 API calls (mocked) |
| `test_models.py` | Data Models (§3) | Pydantic validation, enum values, request schema |
| `test_enums.py` | Enums (§3) | RequestStatus, WorkflowStatus, DAGStatus, BlockStatus, CleanupPolicy, NodeRole |
| `test_stepchain.py` | StepChain Parser | StepChain request parsing, manifest generation |
| `test_sandbox.py` | Sandbox Builder | Sandbox creation, test pset generation |
| `test_output_lfn.py` | Output LFN Helpers | LFN-to-PFN conversion, merged/unmerged LFN generation |
| `test_pilot_runner.py` | Pilot Runner (§5) | FJR parsing, summary computation, script generation |
| `test_pilot_metrics.py` | Pilot Metrics (§5) | Pilot metrics JSON parsing (events/sec, memory, output size) |
| `test_simulator.py` | cmsRun Simulator | CPU burning, memory allocation, ROOT file creation, FJR XML generation |

### 10.3 Known Gaps

The following components lack unit test coverage:

| Component | File | Why it matters |
|---|---|---|
| Adaptive optimizer | `src/condora/core/adaptive.py` | Complex resource optimization math; has had production bugs (enriched metrics ignored, memory sizing wrong) |
| Schedd selector | `src/condora/core/schedd_selector.py` | Multi-schedd selection logic; newly added |

Several test files listed in §10.2 contain utility functions but no `def test_*` functions. These are used by the matrix runner (§5) and do not contribute to T1 pass/fail counts.

---

## 11. Integration Test Coverage

Integration tests (`tests/integration/`) validate cross-component interactions with real services.

### 11.1 Test File Map

| File | Infrastructure | What It Tests |
|---|---|---|
| `test_repository.py` | PostgreSQL | CRUD operations on requests, workflows, processing blocks, DAGs |
| `test_api.py` | PostgreSQL | FastAPI HTTP endpoints with real DB, mocked external services |
| `test_dag_planner.py` | Python | End-to-end DAG planning: InputFiles → splitting → DAG files on disk |
| `test_condor_submit.py` | HTCondor (Level 2) | Real DAG submission and execution on a live pool |
| `test_resource_utilization.py` | HTCondor (Level 2) | CPU utilization verification via `/proc/stat` sampling |
| `test_processing_blocks.py` | PostgreSQL | OutputManager + Repository end-to-end with real DB, mock DBS/Rucio |

Integration tests requiring HTCondor are gated by `@pytest.mark.level2` and are skipped when no pool is available.

### 11.2 Known Gaps

Two integration test files (`test_adaptive_rounds.py`, `test_production_pipeline.py`) are standalone scripts with `__main__` blocks rather than pytest test functions. They work as manual E2E scripts but are not collected by `pytest tests/integration/`. Converting them to pytest format with `@pytest.mark.level2` gating would bring them into the T2b suite.

Four integration test files (`test_dag_planner.py`, `test_condor_submit.py`, `test_resource_utilization.py`, `test_processing_blocks.py`) have test functions that are currently failing due to production code drift.

---

## 12. Environment Verification

Environment checks (`tests/environment/checks.py`) are organized into four capability levels. Each check function returns `(ok: bool, detail: str)`.

### 12.1 Level 0 — Python and Packages

| Check | Function | What |
|---|---|---|
| Python version | `check_python_version()` | Python >= 3.11 |
| Core packages | `check_package_importable()` | fastapi, sqlalchemy, pydantic, pydantic_settings, httpx, pytest, alembic, asyncpg |
| PostgreSQL | `check_postgres_reachable()` | Database accepts connections |

### 12.2 Level 1 — CMSSW Prerequisites

| Check | Function | What |
|---|---|---|
| CVMFS | `check_cvmfs_mounted()` | `/cvmfs/cms.cern.ch` mounted |
| CMSSW releases | `check_cmssw_release_available()` | At least one release directory exists |
| X509 proxy | `check_x509_proxy()` | Valid, non-expired proxy certificate |
| Siteconf | `check_siteconf()` | `site-local-config.xml` exists |
| Apptainer | `check_apptainer()` | `apptainer` or `singularity` in PATH |

### 12.3 Level 2 — HTCondor

| Check | Function | What |
|---|---|---|
| CLI tools | `check_condor_binaries()` | condor_status, condor_submit, condor_q, condor_submit_dag |
| Schedd | `check_condor_schedd()` | Scheduler daemon reachable |
| Slots | `check_condor_slots()` | Pool has available execution slots |
| Python bindings | `check_condor_python_bindings()` | `htcondor2` importable |

### 12.4 Level 3 — Production Services

| Check | Function | What |
|---|---|---|
| ReqMgr2 | `check_reqmgr2_reachable()` | CMS Request Manager API responds |
| DBS | `check_dbs_reachable()` | Data Bookkeeping System API responds |
| CouchDB | `check_couchdb_reachable()` | ConfigCache API responds |

Level 3 checks use `curl` with X509 proxy authentication because Python's `ssl` module does not handle X509 proxy certificate chains correctly.

### 12.5 Matrix Environment Detection

The matrix runner uses `tests/matrix/environment.py` to map these checks into capabilities:

| Capability | Required Checks |
|---|---|
| `condor` | schedd reachable, slots available, Python bindings |
| `cvmfs` | CVMFS mounted |
| `siteconf` | site-local-config exists |
| `apptainer` | apptainer/singularity available |
| `x509` | valid proxy |
| `postgres` | PostgreSQL reachable |
| `reqmgr2` | ReqMgr2 API responds |
| `dbs` | DBS API responds |

Each `WorkflowDef` declares its `requires` tuple. The runner skips workflows whose requirements are not met, reporting them as "skipped" with the reason.

---

## 13. Performance Baselines

The matrix reporter tracks performance metrics that correspond to the success criteria in main spec §13.1:

| Metric | Target | How Tested |
|---|---|---|
| DAG submission rate | 50 DAGs/hour (10K+ nodes/hour) | Scale test with 300.x workflows |
| Tracking latency | < 60 seconds | Timing from DAGMan status change to Condora update |
| API response time | p99 < 500ms | Integration test `test_api.py` timing |
| Adaptive convergence | Within 20% by round 2 | Adaptive workflows 350.x–392.x, R1 vs R2 comparison |

The reporter computes **events per core-hour** as the primary throughput metric:

```
core_hours = request_cpus * sum(per_job_wall_sec) / 3600
ev_core_hour = total_events / core_hours
```

For adaptive workflows, this is computed per round, with delta percentages showing improvement.

---

## 14. Test Automation

Condora is developed by a single developer on a dedicated VM that has all required infrastructure (HTCondor, PostgreSQL, CVMFS, apptainer). Test automation leverages git hooks on this machine rather than external CI services.

### 14.1 Automated Gates

| Mechanism | Trigger | What it runs | Runtime |
|---|---|---|---|
| **Git pre-push hook** | Before every `git push` | T0 (ruff) + T1 (unit tests) | ~20 sec |
| **Nightly cron** | Daily at 02:00 | T2b (integration) + T3 (smoke E2E) | ~6 min |

The pre-push hook is the critical gate — it prevents broken code from entering the repository. If T0 or T1 fails, the push is rejected. The developer fixes the issue and retries.

The nightly run catches integration and E2E regressions that accumulate from multiple commits during the day. Results are logged to a file; failures are surfaced at next session start.

### 14.2 Pre-Push Hook Setup

```bash
# .git/hooks/pre-push
#!/bin/bash
set -e
source .venv/bin/activate
echo "Running T0: lint..."
ruff check src/ tests/
echo "Running T1: unit tests..."
pytest tests/unit/ -q --tb=line
echo "All gates passed."
```

### 14.3 Manual Triggers

| Tier | When to run | Command |
|---|---|---|
| T2a (component) | After merge/cleanup/adaptive changes | `pytest tests/component/` |
| T2b (integration) | After DB/API changes | `pytest tests/integration/` |
| T3 (smoke E2E) | After DAG planner or error handler changes | `python -m tests.matrix -l smoke` |
| T4 (full E2E) | Before commissioning new request type | `python -m tests.matrix -l full` |

### 14.4 Future Extensions

When the team grows beyond a single developer, the automation model should be revisited:

- **Multi-developer**: Add a shared CI service (local Jenkins, self-hosted GitHub Actions runner on the dev VM, or similar) to enforce gates on all contributors
- **Performance regression detection**: Track ev/core-hour from T3 smoke runs across commits, alert on >10% regression
- **Load testing**: Scale test with 300 DAGs × 10K nodes to validate main spec §13.1 targets
- **Simulator profile library**: Curated step profiles calibrated against production measurements for deterministic T3 testing

---

## 15. Known Gaps and Policy

### 15.1 Current Test Health

As of 2026-03-15:

| Suite | Collected | Passing | Failing | Pass rate |
|---|---|---|---|---|
| Unit (`tests/unit/`) | 469 | 372 | 97 | 79% |
| Integration (`tests/integration/`) | 33 | 20 | 13 | 61% |
| Environment (`tests/environment/`) | 22 | 22 | 0 | 100% |

Root causes of failures are tracked in the implementation backlog. Most failures are caused by production code evolving (multi-schedd API, accounting group requirement, enriched WU format) without corresponding test updates.

### 15.2 Untested Modules

| Module | Lines | Risk | Notes |
|---|---|---|---|
| `adaptive.py` | ~1,660 | High — complex math, documented bugs | Only validated via T4 E2E |
| `schedd_selector.py` | ~40 | Low | Newly added for multi-schedd |

### 15.3 Test Files Without Test Functions

Several test files in `tests/unit/` and `tests/integration/` contain utility code or standalone scripts but no `def test_*` functions that pytest collects. These files provide helper functions used by the matrix runner or serve as manual integration scripts, but they do not contribute to T1 or T2b test counts.

### 15.4 Policy

1. **Zero-failure rule**: T0 + T1 must pass with zero failures before any commit. A test that fails because production code changed must be fixed or deleted in the same commit.
2. **New modules ship with tests**: Any new file in `src/condora/core/` or `src/condora/adapters/` must include unit tests.
3. **testing.md tracks reality**: This document is updated when test infrastructure changes. §15.1 test health numbers are updated periodically.
4. **Test data catalog maintained**: After each T4 run that exercises a new request type or code path, relevant artifacts are captured into the test data catalog (§4.5).
