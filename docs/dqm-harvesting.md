# DQM Harvesting in CMS: Design Notes for Condora

## Overview

Data Quality Monitoring (DQM) is a two-phase process that produces histograms
characterizing detector performance and physics object quality. These histograms
are uploaded to the DQM GUI for visual inspection, comparison across runs, and
data certification.

Condora needs to support DQM eventually — both for data reprocessing (where DQM is
mandatory) and for MC production (where it is done on demand for release
validation and targeted quality checks).

## How DQM Works in CMSSW

### Phase 1: DQM Production (inline with RECO)

DQM runs as part of the same `cmsRun` process as reconstruction. When
`--step RAW2DIGI,RECO,DQM` is specified in cmsDriver.py, the DQM analyzer
modules (`DQMEDAnalyzer` subclasses) are added to the same process schedule
after the RECO path. They read reconstructed objects (tracks, jets, muons,
etc.) directly from the Event in memory — there is no intermediate file I/O.

The DQM analyzers book and fill histograms (called Monitor Elements) using
the `DQMStore` service. These are written out by `DQMRootOutputModule` in
**DQMIO** format (TTree-based ROOT files).

A typical cmsDriver command producing DQMIO:

```
cmsDriver.py step3 \
  --step RAW2DIGI,L1Reco,RECO,DQM:@standardDQM+@miniAODDQM \
  --eventcontent AODSIM,DQM \
  --datatier AODSIM,DQMIO \
  --conditions auto:phase1_2024_realistic \
  --filein file:step2.root
```

This produces two output files from a single cmsRun: AODSIM and DQMIO.

### Phase 2: DQM Harvesting (separate step after merge)

Harvesting is a separate cmsRun that reads the DQMIO files from all jobs
in a run, merges the histograms, runs post-processing (`DQMEDHarvester`
modules that compute efficiencies, ratios, fits, quality tests), and writes
a single legacy-format DQM ROOT file (TDirectory-based).

```
cmsDriver.py step4 \
  --step HARVESTING:@standardDQM+@miniAODDQM \
  --scenario pp --filetype DQM \
  --conditions auto:phase1_2024_realistic --mc \
  --filein file:step3_inDQM.root \
  -n -1
```

The `--filetype DQM` flag tells the framework to use `DQMRootSource` instead
of `PoolSource` for reading DQMIO input.

### Output File Naming

Harvested DQM ROOT files follow a strict naming convention:

```
DQM_V0001_R000XXXXXX__<PrimaryDataset>__<ProcessedDataset>__<DataTier>.root
```

For multi-run harvesting, run number 999999 is used as a convention.

### VALIDATION vs DQM

CMSSW has two distinct histogram-producing frameworks:

- **DQM** (`DQMOffline/` packages): Monitors detector and physics object
  quality using only reconstructed quantities. Runs identically on data and MC
  (with minor config differences). Produces histograms like track multiplicity,
  jet pT spectra, muon segment hit counts.

- **VALIDATION** (`Validation/` packages): Compares RECO objects against MC
  truth (GenParticles, TrackingParticles, SimVertices). MC-only by definition.
  Produces histograms like tracking efficiency vs pT.

Both produce DQMIO output and are harvested together in the same step4.

## DQM Sequences

DQM sequences are defined in `DQMOffline/Configuration/python/autoDQM.py`
as a dictionary mapping names to `[production, post-processing, harvesting]`
tuples. The `@` prefix in step specifications resolves through this dictionary.

Key sequences:

| Name | Use Case |
|------|----------|
| `@allForPrompt` | Data prompt reco / reprocessing (full DPG + POG + HLT) |
| `@standardDQM` | RelVal (full standard DQM) |
| `@miniAODDQM` | MiniAOD-level monitoring (subset) |
| `@nanoAODDQM` | NanoAOD-level monitoring (subset) |
| `DQMOfflinePOGMC` | MC POG-only (vertex, b-tagging, physics objects) |
| `@ExtraHLT` | Additional HLT monitoring |

For MC, lighter sequences like `DQMOfflinePOGMC` are typical. The full
`@allForPrompt` suite includes detector-level monitoring that is mainly
relevant for data.

## Current CMS Practice

### Data Reprocessing (DQM is mandatory, inline)

DQM is included in the RECO step because data certification depends on it:

```
--step RAW2DIGI,L1Reco,RECO,DQM:@allForPrompt
--eventcontent AOD,MINIAOD,DQM
--datatier AOD,MINIAOD,DQMIO
```

The DQMIO files are then merged per-run and harvested. WMAgent handles this
via `EnableHarvesting=True` — when a merge task produces DQMIO output, a child
DQM harvest task is automatically created.

### MC Central Production (no DQM by default)

Standard MC StepChains do not include the DQM step:

```
--step RAW2DIGI,L1Reco,RECO
--eventcontent AODSIM
--datatier AODSIM
```

Request specs have `EnableHarvesting=False`, `DQMConfigCacheID=None`,
`DQMSequences=[]`. No DQMIO is produced.

### MC Release Validation / On-Demand (DQM included)

For RelVal and targeted validation campaigns, DQM and VALIDATION are included
inline with RECO:

```
--step RAW2DIGI,L1Reco,RECO,RECOSIM,PAT,VALIDATION:@standardValidation+@miniAODValidation,DQM:@standardDQM+@ExtraHLT+@miniAODDQM+@nanoAODDQM
--eventcontent RECOSIM,MINIAODSIM,DQM
--datatier GEN-SIM-RECO,MINIAODSIM,DQMIO
```

This is the only way to get DQM for MC because intermediate tiers
(GEN-SIM-RAW) are not kept in normal production — if DQM is not run inline
with RECO, it cannot be produced later without re-running the entire chain.

### Summary

| Workflow Type | DQM Inline? | VALIDATION? | DQMIO Produced? | Harvesting? |
|---------------|-------------|-------------|-----------------|-------------|
| Data prompt reco | Yes | No | Yes | Yes (automatic) |
| Data reprocessing | Yes | No | Yes | Yes (automatic) |
| MC central production | No | No | No | No |
| MC RelVal | Yes | Yes | Yes | Yes (separate step) |
| MC targeted validation | Yes | Optional | Yes | Yes |

## How WMAgent Handles DQM

### Inline DQM + Automatic Harvesting

In `StdBase.addMergeTask()`, when `EnableHarvesting=True` and a merge task
produces output with data tier `DQMIO` or `DQM`, a child DQM harvest task is
automatically created via `addDQMHarvestTask()`. The harvest task has three
steps:

1. **cmsRun1** (CMSSW) — runs the harvesting configuration:
   `EDMtoMEConverter` → harvesting analyzers → `DQMFileSaver`
2. **upload1** (DQMUpload) — uploads the harvested ROOT file to the DQM GUI
3. **logArch1** (LogArchive) — archives logs

### DQMUpload Executor

The upload step (`WMCore/WMSpec/Steps/Executors/DQMUpload.py`):

1. Scans the CMSSW step's Framework Job Report for analysis files with
   `FileClass == "DQM"` (registered by CMSSW's `DQMFileSaver` module)
2. Computes MD5 checksum of each file
3. Sends HTTPS POST (multipart/form-data) to `<DQMUploadUrl>/data/put`
4. Authenticates with X.509 proxy certificate
5. Checks `Dqm-Status-Code` response header for success/failure

### Standalone DQMHarvest Request

WMCore also supports a dedicated `RequestType = "DQMHarvest"` that reads
from an existing DQMIO dataset. This is used for harvesting without a parent
production workflow.

### Upload Destinations

| Instance | URL |
|----------|-----|
| Development | `https://cmsweb.cern.ch/dqm/dev` |
| Offline (production) | `https://cmsweb.cern.ch/dqm/offline` |
| RelVal | `https://cmsweb.cern.ch/dqm/relval` |
| Online | `https://cmsweb.cern.ch/dqm/online` |

### New DQM GUI (migration in progress)

The new DQM GUI uses a two-stage upload:
1. Copy ROOT file to EOS at `/eos/cms/store/group/comm_dqm/DQMGUI_data/`
2. Register via HTTP POST to `<url>/api/v1/register` with JSON metadata

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EnableHarvesting` | `False` | Auto-attach harvest to merge of DQMIO output |
| `DQMUploadUrl` | `https://cmsweb.cern.ch/dqm/dev` | Upload destination |
| `DQMUploadProxy` | `None` | X.509 proxy for authentication |
| `DQMConfigCacheID` | `None` | Cached CMSSW config for harvesting |
| `DQMHarvestUnit` | `byRun` | `byRun` (one job/run) or `multiRun` (all runs) |
| `DQMSequences` | `[]` | Which DQM sequences to run |

## Implications for Condora

### The Core Problem

DQM can only be produced inline with RECO because it reads reconstructed
objects from memory. The intermediate input tier (GEN-SIM-RAW or equivalent)
is not kept in normal MC production. This means:

- If DQM is not included at RECO time, it can never be produced later
  without re-running the entire chain from scratch
- For MC central production, CMS currently accepts this trade-off
- For RelVal and targeted validation, DQM is included explicitly

### What Condora Needs to Support

1. **Request-level DQM configuration**: The request spec should be able to
   specify whether DQM should be included in the RECO step, and which DQM
   sequences to run. This is already present in the ReqMgr2 schema
   (`DQMSequences`, `DQMConfigCacheID`, `EnableHarvesting`).

2. **DQMIO as an additional output tier**: When DQM is enabled, the RECO
   step's cmsDriver command gets `DQM:<sequences>` appended to `--step` and
   `DQM` added to `--eventcontent` / `DQMIO` added to `--datatier`. This
   produces an additional output file per job.

3. **DQMIO merge**: DQMIO files need special merge handling — they use
   `DQMRootSource` instead of `PoolSource`. WMAgent sets `newDQMIO=True`
   in the merge configuration for DQMIO tiers.

4. **Harvesting step**: After DQMIO files are merged (per-run), a harvesting
   cmsRun reads them with `--filetype DQM` and produces the final harvested
   ROOT file.

5. **Upload to DQM GUI**: The harvested file is uploaded via HTTPS POST with
   X.509 authentication. Condora would need proxy certificate handling.

### Suggested Approach

DQM support does not need to change the core Condora architecture. It is
essentially:

- An optional extra output module on the RECO step (DQMIO)
- A specialized merge for DQMIO files (different source module)
- A post-merge harvesting step (cmsRun reading DQMIO, writing legacy ROOT)
- An upload action (HTTPS POST to DQM GUI)

The DAG structure would add:

```
[existing work units] → [DQMIO merge per run] → [harvest] → [upload]
```

This is downstream of the main processing chain and does not affect the
core processing/merge/cleanup flow.

### Open Questions

1. Should Condora support DQM by default for all MC production, or only when
   explicitly requested? Adding DQM to every RECO step adds CPU overhead
   and DQMIO storage, but avoids the inability to produce DQM later.

2. How should DQMIO merge be organized? Per-run (standard) or multi-run?
   For MC, all events typically belong to run 1, so per-run is effectively
   merging all DQMIO files into one.

3. Should harvesting and upload be part of the same DAG, or a separate
   workflow triggered after the main processing completes?

4. What DQM sequences should be the default for MC? `DQMOfflinePOGMC`
   is lightweight, `@standardDQM` is comprehensive.

5. How to handle X.509 proxy certificates for DQM upload in Condora's
   operational model?
