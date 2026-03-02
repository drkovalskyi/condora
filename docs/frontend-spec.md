# WMS2 Web Frontend Specification

| Field           | Value                          |
|-----------------|--------------------------------|
| Version         | 0.1                            |
| Status          | Draft                          |
| Date            | 2026-03-01                     |
| Audience        | CMS operators and developers   |

## Table of Contents

1. [Overview](#1-overview)
2. [Technology Stack](#2-technology-stack)
3. [Architecture](#3-architecture)
4. [Authentication and Authorization](#4-authentication-and-authorization)
5. [Pages and Views](#5-pages-and-views)
6. [Data Refresh and Live Updates](#6-data-refresh-and-live-updates)
7. [Charts and Visualization](#7-charts-and-visualization)
8. [API Integration](#8-api-integration)
9. [Error Handling and UX](#9-error-handling-and-ux)
10. [Visual Design](#10-visual-design)
11. [Implementation Plan](#11-implementation-plan)

---

## 1. Overview

### 1.1 Purpose

The WMS2 web frontend provides a browser-based interface for monitoring and managing CMS workflow processing. It replaces the CLI monitoring loop with a persistent, multi-user dashboard and gives operators direct access to all management actions (stop, release, restart, site bans) without requiring shell access.

### 1.2 Goals

- **Visibility**: Real-time overview of all active requests, workflows, DAGs, and sites
- **Operability**: All API actions accessible through the UI with confirmation dialogs
- **Debuggability**: Full drill-down from request to individual HTCondor jobs, with error summaries and log access
- **Low maintenance**: Minimal dependencies, no complex build toolchain, easy to extend
- **Prototype-first**: Ship a working UI quickly; polish later

### 1.3 Audience

- **Operators**: Monitor production workflows, take corrective actions (stop, release, restart, ban sites), track progress toward completion
- **Developers**: Debug failures by drilling into DAG details, HTCondor job state, step-level metrics, error patterns

---

## 2. Technology Stack

### 2.1 Frontend

| Component       | Choice                         | Rationale                                    |
|-----------------|--------------------------------|----------------------------------------------|
| Language        | JavaScript (ES modules)        | No build step, runs directly in browser      |
| UI library      | Alpine.js + htmx               | Lightweight reactivity without a build pipeline. Alpine for component state, htmx for server-driven updates |
| Charts          | Chart.js                       | Single-file CDN include, good defaults, interactive |
| CSS framework   | Pico CSS or Simple.css         | Classless or minimal-class CSS. Clean modern look out of the box |
| Icons           | Lucide (SVG icons via CDN)     | Lightweight, consistent icon set             |

**No build step required.** All dependencies loaded via CDN `<script>` tags or vendored into `static/vendor/`. No npm, no webpack, no node_modules.

### 2.2 Backend (serving)

The frontend is served as static files by the existing FastAPI application. No separate web server needed.

```
src/wms2/
  static/           # JS, CSS, vendor libs
    js/
      app.js         # Main application logic
      api.js         # API client wrapper
      charts.js      # Chart configurations
      components/    # Alpine.js components
    css/
      style.css      # Custom styles
    vendor/          # Vendored CDN libs (Alpine, htmx, Chart.js, etc.)
  templates/         # Jinja2 HTML templates
    base.html        # Layout with nav, auto-refresh controls
    dashboard.html   # Landing page
    requests.html    # Request list
    request_detail.html
    workflow_detail.html
    dag_detail.html
    sites.html
    site_detail.html
    import.html
    activity.html
    errors.html
```

FastAPI serves:
- `GET /` and `GET /ui/*` — HTML pages (Jinja2 templates)
- `GET /static/*` — JS, CSS, vendor files
- `GET /api/v1/*` — existing REST API (consumed by the frontend JS)

### 2.3 Why not React/Vue/Angular?

This is a prototype for a small team. The priorities are:
1. **Zero build toolchain** — no npm install, no webpack config, no node version management
2. **Easy to modify** — any developer can edit HTML/JS files and reload the browser
3. **Long-term maintainability** — no framework version upgrades, no breaking changes from dependencies
4. **Small footprint** — total frontend code should be < 5000 lines

If the prototype grows into a production system with many contributors, migrating to a framework is straightforward since the API layer is clean REST.

---

## 3. Architecture

### 3.1 Data Flow

```
Browser
  ├── Page load: GET /ui/{page} → FastAPI renders Jinja2 template → HTML
  ├── Data fetch: JS calls GET /api/v1/* → JSON → Alpine.js updates DOM
  ├── Actions: JS calls POST/PATCH/DELETE /api/v1/* → JSON response → update UI
  └── Auto-refresh: setInterval polls API endpoints → updates components
```

### 3.2 State Management

No client-side state store. Each page/component fetches its own data from the API. Alpine.js `x-data` holds local component state (current filters, sort order, expanded rows). The API is the single source of truth.

### 3.3 URL Routing

Server-side routing via FastAPI. Each page is a distinct URL:

| URL Pattern                    | Page                    |
|-------------------------------|-------------------------|
| `/ui/`                        | Dashboard               |
| `/ui/requests`                | Request list            |
| `/ui/requests/{name}`         | Request detail          |
| `/ui/workflows/{id}`          | Workflow detail         |
| `/ui/dags/{id}`               | DAG detail              |
| `/ui/sites`                   | Site list               |
| `/ui/sites/{name}`            | Site detail             |
| `/ui/import`                  | Import request form     |
| `/ui/activity`                | Global activity feed    |
| `/ui/errors`                  | Attention needed view   |

Pages are full HTML documents (not SPA fragments). Navigation uses normal `<a>` links. This keeps the URL bar accurate, enables browser back/forward, and allows bookmarking.

---

## 4. Authentication and Authorization

### 4.1 Authentication: CERN SSO (IAM)

WMS2 integrates with CERN's Keycloak-based IAM (Identity and Access Management) using OpenID Connect (OIDC).

**Flow:**
1. User visits `/ui/` → FastAPI checks for valid session cookie
2. If no session → redirect to CERN IAM login page
3. User authenticates via CERN SSO → CERN IAM redirects back with authorization code
4. FastAPI exchanges code for tokens (ID token + access token)
5. FastAPI creates a session cookie, stores user identity (username, groups)
6. Subsequent requests include session cookie → FastAPI validates session

**Implementation:**
- Use `authlib` Python library for OIDC client
- Store sessions server-side (in-memory dict or database table)
- Session timeout: 8 hours (configurable)
- API endpoints also accept Bearer tokens for programmatic access

### 4.2 Authorization: Two Roles

| Role       | Permissions                                                      |
|------------|------------------------------------------------------------------|
| **Viewer** | Read all data. No mutations. Cannot stop, release, fail, restart, ban, import. |
| **Operator** | All Viewer permissions + all write actions (stop, release, fail, restart, ban/unban, import, update priority) |

**Role assignment:**
- Map CERN e-groups to roles. For example:
  - `cms-comp-ops` → Operator
  - All authenticated users → Viewer
- Configurable via WMS2 settings (not hardcoded)

**UI behavior:**
- Viewers see all data but action buttons are hidden or disabled
- Operators see all data and all action buttons
- Every mutation records `who` (username from session) in the audit log

### 4.3 Development Mode

For local development and testing, auth can be disabled via configuration:

```python
# settings
auth_enabled: bool = True
auth_dev_user: str = "developer"  # used when auth_enabled=False
```

When auth is disabled, all requests are treated as Operator role with the dev username.

---

## 5. Pages and Views

### 5.1 Dashboard (Landing Page)

**URL:** `/ui/`

**Layout:**

```
┌─────────────────────────────────────────────────────────────┐
│  WMS2 Dashboard                          [Refresh] [15s ▾]  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐          │
│  │ Active  │ │ Queued  │ │  Held   │ │ Failed  │          │
│  │   12    │ │    3    │ │    1    │ │    0    │          │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘          │
│                                                             │
│  ┌─────────────┐  ┌──────────────────────────────────────┐ │
│  │ Admission   │  │  Events Progress (chart)             │ │
│  │ 12/300 DAGs │  │  ████████░░░░ 65%                    │ │
│  │ Capacity: ✓ │  │  [stacked bar: produced/remaining]   │ │
│  └─────────────┘  └──────────────────────────────────────┘ │
│                                                             │
│  Lifecycle Manager: ● Running (cycle: 30s)                  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Active Requests                        [Filter ▾]   │   │
│  ├──────┬──────────┬──────┬────────┬──────┬────────────┤   │
│  │Status│ Name     │Round │Progress│ Prio │ Age        │   │
│  ├──────┼──────────┼──────┼────────┼──────┼────────────┤   │
│  │ ● ACT│ B2G-Run3.│  1   │ ██░ 45%│100000│ 2h 15m     │   │
│  │ ● ACT│ HIG-Run3.│  0   │ ░░░  5%│ 90000│ 45m        │   │
│  │ ● HLD│ TOP-Run3.│  1   │ ██░ 30%│110000│ 5h 20m     │   │
│  └──────┴──────────┴──────┴────────┴──────┴────────────┘   │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Recent Activity                                      │   │
│  │ 14:32  B2G-Run3.. round 1 started (12 work units)   │   │
│  │ 14:28  HIG-Run3.. pilot completed (round 0 → 1)     │   │
│  │ 14:15  TOP-Run3.. held — site exclusion T2_US_MIT    │   │
│  │ 14:02  SUS-Run3.. completed (100% events produced)   │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

**Components:**

1. **Status cards**: Counts of requests by status (active, queued, held, failed). Clicking a card filters the request table to that status.
2. **Admission panel**: Active DAGs vs capacity, queue depth, next pending request name.
3. **Events progress chart**: Aggregate stacked bar — total events produced vs target across all active requests.
4. **Lifecycle manager indicator**: Running/stopped, cycle interval.
5. **Active requests table**: Sortable, filterable. Columns: status badge, request name (link to detail), current round, progress bar (%), priority, age (time since creation). Click row → request detail page.
6. **Recent activity feed**: Last ~20 events across all requests. Timestamp, request name, event description. Auto-scrolls. Click entry → relevant detail page.

**Data sources:**
- Status cards: `GET /api/v1/status`
- Admission: `GET /api/v1/admission/queue`
- Lifecycle: `GET /api/v1/lifecycle/status`
- Request table: `GET /api/v1/requests?limit=50`
- Activity feed: `GET /api/v1/activity/recent` (new endpoint — see Section 8)

### 5.2 Request List

**URL:** `/ui/requests`

Full request table with filtering and sorting. Unlike the dashboard (which shows only active requests), this shows all requests including completed and failed.

**Filters:**
- Status dropdown (multi-select): new, submitted, queued, pilot_running, planning, active, stopping, resubmitting, held, completed, partial, failed, aborted
- Campaign text filter
- Priority range
- Date range (created_at)
- Text search on request name

**Columns:** Status, Name, Campaign, Splitting, Round, Progress (%), Events Produced / Target, Priority, Urgent flag, Created, Updated

**Actions (bulk):**
- Select multiple → Bulk stop, Bulk update priority

### 5.3 Request Detail

**URL:** `/ui/requests/{name}`

**Layout:**

```
┌─────────────────────────────────────────────────────────────┐
│  ← Requests    B2G-Run3Summer23BPixwmLHEGS-06000           │
│                 Status: ● ACTIVE    Priority: 100000        │
├─────────────────────────────────────────────────────────────┤
│  [Overview] [Workflow] [Errors] [Timeline] [Versions]       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Overview tab:                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Request Info                                        │    │
│  │ Requestor: user123     Campaign: Run3Summer23       │    │
│  │ Splitting: EventBased  Events/Job: 500              │    │
│  │ Input Dataset: /B2G/.../GEN-SIM-RAW                 │    │
│  │ Adaptive: Yes          Cleanup: keep_until_replaced │    │
│  │ Created: 2026-03-01 12:00                           │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Workflow Summary                                    │    │
│  │ Round: 1/∞  Events: 12,500 / 50,000 (25%)          │    │
│  │ Progress: ████████░░░░░░░░░░░░░ 25%                │    │
│  │                                                     │    │
│  │ DAG: ● RUNNING  Nodes: 150 total                   │    │
│  │   Done: 45  Running: 12  Idle: 88  Failed: 3  Held: 2│  │
│  │ Work Units: 8/50 completed                          │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Step Metrics (from pilot / round 0)                 │    │
│  │ ┌──────────┬─────────┬────────┬─────────┬────────┐ │    │
│  │ │ Step     │CPU Eff %│Mem (MB)│Time/Evt │Threads │ │    │
│  │ ├──────────┼─────────┼────────┼─────────┼────────┤ │    │
│  │ │ GEN-SIM  │   85%   │ 2,100  │  12.5s  │   8   │ │    │
│  │ │ DIGI     │   92%   │ 3,400  │   8.2s  │   8   │ │    │
│  │ │ RECO     │   78%   │ 4,200  │  22.1s  │   8   │ │    │
│  │ │ MINIAODSIM│  95%   │ 1,800  │   3.1s  │   8   │ │    │
│  │ │ NANOAODSIM│  97%   │ 1,200  │   1.5s  │   8   │ │    │
│  │ └──────────┴─────────┴────────┴─────────┴────────┘ │    │
│  │ Recommended: 500 events/job, 5120 MB memory         │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Actions:                                                   │
│  [Stop ■] [Update Priority] [Fail ✗] [Restart ↻]          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Tabs:**

1. **Overview**: Request metadata, workflow summary with progress, step metrics table, processing blocks status
2. **Workflow**: Detailed workflow view — DAG node breakdown, category throttles, current DAG link
3. **Errors**: Error summary from `GET /requests/{name}/errors` — grouped by error type, with failure counts, affected sites, sample error messages
4. **Timeline**: Status transition history — a vertical timeline showing every state change with timestamps and duration between transitions
5. **Versions**: Version chain — shows previous and superseding request names with links

**Actions (Operator only):**
- **Stop**: Confirmation dialog with reason text input → `POST /requests/{name}/stop`
- **Update Priority**: Inline editable priority field → `PATCH /requests/{name}`
- **Fail**: Confirmation dialog ("Are you sure? This is irreversible.") → `POST /requests/{name}/fail`
- **Restart**: Confirmation dialog explaining version increment → `POST /requests/{name}/restart`
- **Release** (shown when status=held): → `POST /requests/{name}/release`

### 5.4 Workflow Detail

**URL:** `/ui/workflows/{id}`

Linked from the request detail page. Shows:

- Workflow metadata (splitting, config_data, current_round)
- Current DAG status with node count breakdown
- Processing blocks table with status (open/complete/archived/failed/invalidated)
- Step metrics charts (CPU efficiency, memory, time per event — per step, per round)
- HTCondor info: pilot_cluster_id, schedd, link to DAG detail

### 5.5 DAG Detail

**URL:** `/ui/dags/{id}`

Linked from workflow detail. Shows:

- DAG status, file paths (dag_file, submit_dir, rescue_dag)
- Node counts: idle, running, done, failed, held — as both numbers and a stacked bar chart
- Work unit progress: completed_work_units / total_work_units
- Node-level table (requires `GET /dags/{id}/nodes` — new endpoint):
  - Node name, role (Processing/Merge/Cleanup), status, retry count, site, duration
  - Click node → shows HTCondor job details (cluster.proc, stdout/stderr log paths)
- DAG history timeline: status transitions with node count snapshots at each point
- Recovery chain: link to parent DAG (if rescue) or child DAGs

**HTCondor integration:**
- Show `condor_q` output for this DAG's cluster (via new API endpoint or live query)
- Link to job log files (stdout, stderr, HTCondor log) — served via API or direct file path
- Show held job reasons (from `condor_q -hold`)

### 5.6 Site Management

**URL:** `/ui/sites`

**Layout:**

```
┌─────────────────────────────────────────────────────────────┐
│  Sites                                        [Refresh]     │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────┬────────┬──────────┬──────────┬────────────┐   │
│  │ Site     │ Status │ Slots    │ Bans     │ Actions    │   │
│  ├──────────┼────────┼──────────┼──────────┼────────────┤   │
│  │ T2_US_MIT│●Enabled│ 45/120/10│ 1 active │ [Ban][Drain]│  │
│  │ T2_DE_..│●Enabled│ 80/200/5 │ 0        │ [Ban][Drain]│  │
│  │ T1_US_..│●Drain  │ 20/500/0 │ 0        │ [Enable]   │  │
│  └──────────┴────────┴──────────┴──────────┴────────────┘   │
│                                                             │
│  Slots format: running / total / pending                    │
│                                                             │
│  Active Bans:                                               │
│  ┌────────────┬────────────┬──────────┬──────────┬───────┐  │
│  │ Site       │ Reason     │ Workflow │ Expires  │       │  │
│  ├────────────┼────────────┼──────────┼──────────┼───────┤  │
│  │ T2_US_MIT  │ Infra fail │ B2G-Run3.│ 2h left  │[Unban]│  │
│  └────────────┴────────────┴──────────┴──────────┴───────┘  │
└─────────────────────────────────────────────────────────────┘
```

**Site detail page** (`/ui/sites/{name}`):
- Site configuration (schedd, collector, storage element, storage path)
- Slot utilization chart over time
- Ban history (active and expired)
- Workflows currently using this site

**Actions (Operator only):**
- **Ban**: Dialog with reason, optional workflow scope, optional duration → `POST /sites/{name}/ban`
- **Unban**: Confirmation → `DELETE /sites/{name}/ban`
- **Drain**: Set drain_until → `POST /sites/{name}/drain` (needs API implementation)
- **Enable**: Re-enable site → `POST /sites/{name}/enable` (needs API implementation)

### 5.7 Import Request

**URL:** `/ui/import`

**Form fields:**
1. **Request name** (text input, required): Paste a ReqMgr2 request name (e.g., `cmsunified_task_B2G-...`)
2. **Sandbox mode** (dropdown): `cmssw` (default), `singularity`, `custom`
3. **Test fraction** (number input, optional): 0.01–1.0. Reduces events for testing.
4. **Events per job** (number input, optional): Override default splitting
5. **Files per job** (number input, optional): Override default splitting
6. **Max files** (number input, optional): Limit input files
7. **Dry run** (checkbox): Plan DAG but do not submit

**Behavior:**
1. User fills form, clicks "Import"
2. Frontend calls `POST /api/v1/import` (new endpoint — wraps CLI import logic)
3. Show progress: "Fetching from ReqMgr2..." → "Planning DAG..." → "Submitting..."
4. On success: redirect to the new request's detail page
5. On error: show error message with details

### 5.8 Errors / Attention Needed

**URL:** `/ui/errors`

Aggregates problems across all requests that need operator attention.

**Sections:**
1. **Held requests**: Requests in `held` status with error summary and suggested actions (release, fail, restart)
2. **Failed DAGs**: DAGs that failed with error details (last failure reason, affected nodes)
3. **Active site bans**: Sites currently banned with ban details
4. **Stuck requests**: Requests that haven't made progress in > N minutes (configurable threshold)

Each item links to the relevant detail page for investigation.

### 5.9 Global Activity Feed

**URL:** `/ui/activity`

Chronological feed of system events, most recent first. Filterable by:
- Event type (status transition, DAG submission, error, operator action, round completion)
- Request name
- Time range

**Event types displayed:**
- Request status transitions (new → submitted → active → completed)
- DAG submissions and completions
- Round completions with metrics summary
- Errors and failures
- Operator actions (stop, release, fail, restart, ban, unban) with username
- Site ban creation and expiry

**Data source:** `GET /api/v1/activity` (new endpoint — see Section 8)

---

## 6. Data Refresh and Live Updates

### 6.1 Auto-Refresh

The UI auto-refreshes data at a configurable interval (default: 15 seconds).

**Implementation:**
- A global `setInterval` timer calls the relevant API endpoints for the current page
- Alpine.js reactively updates the DOM when data changes
- A visual indicator shows the countdown to the next refresh
- User can pause/resume auto-refresh
- User can change the interval (5s, 10s, 15s, 30s, 60s) via dropdown

**Per-page refresh targets:**
| Page            | Endpoints polled                                           |
|-----------------|------------------------------------------------------------|
| Dashboard       | `/status`, `/admission/queue`, `/lifecycle/status`, `/requests?status=active,held`, `/activity/recent` |
| Request list    | `/requests` (with current filters)                         |
| Request detail  | `/requests/{name}`, `/workflows/by-request/{name}`        |
| Workflow detail | `/workflows/{id}`, `/dags/{id}` (current DAG)             |
| DAG detail      | `/dags/{id}`, `/dags/{id}/history`, `/dags/{id}/nodes`    |
| Sites           | `/sites`, `/sites/bans/active`                             |
| Errors          | `/requests?status=held`, `/sites/bans/active`, custom stuck detection |

### 6.2 Visual Feedback

- Stale data indicator: if a refresh fails, show a yellow "Data may be stale" banner
- Loading spinners on individual components (not full-page)
- Highlight recently changed values (brief color flash on update)

---

## 7. Charts and Visualization

### 7.1 Chart Library

Chart.js via CDN. Interactive (hover tooltips, click handlers), responsive, supports real-time updates.

### 7.2 Dashboard Charts

1. **Requests by status** (doughnut chart): Visual breakdown of request counts by status category
2. **Aggregate events progress** (stacked horizontal bar): Events produced vs remaining across all active requests
3. **Active DAG node status** (stacked bar): Aggregate node counts (done/running/idle/failed/held) across all active DAGs

### 7.3 Request/Workflow Detail Charts

4. **Events produced over time** (line chart): Time series of events_produced for this workflow. Data points from DAG history snapshots.
5. **Node status over time** (stacked area chart): How node counts (done/running/idle/failed/held) evolve over the DAG's lifetime. Data from DAG history.
6. **Step metrics comparison** (grouped bar chart): CPU efficiency, memory, time per event side-by-side for each processing step
7. **Progress per round** (multi-series line): If multiple rounds, show events produced and efficiency per round

### 7.4 DAG Detail Charts

8. **Node status breakdown** (horizontal stacked bar): Current node counts by status
9. **Work unit completion timeline** (Gantt-style or stepped line): When each work unit completed over time

### 7.5 Site Charts

10. **Slot utilization** (line chart per site): Running/total/pending slots over time
11. **Ban timeline** (Gantt chart): Active bans across sites over time

### 7.6 Data for Charts

Most chart data comes from existing API endpoints. Time-series data requires either:
- **DAG history** (`GET /dags/{id}/history`): Already stores periodic snapshots with node counts
- **New metrics endpoint**: For aggregate time-series data across workflows (see Section 8)

For the prototype, charts can use point-in-time snapshots (re-fetched on each auto-refresh cycle) rather than historical time-series. Historical data can be added later by storing periodic snapshots in a metrics table.

---

## 8. API Integration

### 8.1 Existing Endpoints Used

All endpoints listed in the WMS2 API (Section 7 of the main spec) are consumed by the frontend. The frontend JS wraps them in an `api.js` module:

```javascript
// api.js — thin wrapper around fetch()
const api = {
  requests: {
    list:    (params) => get('/api/v1/requests', params),
    get:     (name)   => get(`/api/v1/requests/${name}`),
    update:  (name, data) => patch(`/api/v1/requests/${name}`, data),
    abort:   (name)   => del(`/api/v1/requests/${name}`),
    stop:    (name, reason) => post(`/api/v1/requests/${name}/stop`, {reason}),
    release: (name)   => post(`/api/v1/requests/${name}/release`),
    fail:    (name)   => post(`/api/v1/requests/${name}/fail`),
    restart: (name)   => post(`/api/v1/requests/${name}/restart`),
    errors:  (name)   => get(`/api/v1/requests/${name}/errors`),
    versions:(name)   => get(`/api/v1/requests/${name}/versions`),
  },
  workflows: {
    list:      (params) => get('/api/v1/workflows', params),
    get:       (id)     => get(`/api/v1/workflows/${id}`),
    byRequest: (name)   => get(`/api/v1/workflows/by-request/${name}`),
    blocks:    (id)     => get(`/api/v1/workflows/${id}/blocks`),
  },
  dags: {
    list:    (params) => get('/api/v1/dags', params),
    get:     (id)     => get(`/api/v1/dags/${id}`),
    history: (id)     => get(`/api/v1/dags/${id}/history`),
    nodes:   (id)     => get(`/api/v1/dags/${id}/nodes`),       // new
  },
  sites: {
    list:      ()     => get('/api/v1/sites'),
    get:       (name) => get(`/api/v1/sites/${name}`),
    bans:      (name) => get(`/api/v1/sites/${name}/bans`),
    allBans:   ()     => get('/api/v1/sites/bans/active'),
    ban:       (name, data) => post(`/api/v1/sites/${name}/ban`, data),
    unban:     (name) => del(`/api/v1/sites/${name}/ban`),
  },
  admission: {
    queue: () => get('/api/v1/admission/queue'),
  },
  lifecycle: {
    status: () => get('/api/v1/lifecycle/status'),
  },
  monitoring: {
    health:  () => get('/api/v1/health'),
    status:  () => get('/api/v1/status'),
    metrics: () => get('/api/v1/metrics'),
  },
};
```

### 8.2 New Endpoints Required

The following new API endpoints are needed to support the frontend:

#### 8.2.1 `POST /api/v1/import`

Import a request from ReqMgr2 (replaces CLI `wms2 import`).

**Request body:**
```json
{
  "request_name": "cmsunified_task_B2G-...",
  "sandbox_mode": "cmssw",
  "test_fraction": 0.01,
  "events_per_job": null,
  "files_per_job": null,
  "max_files": null,
  "dry_run": false
}
```

**Response:** `201 Created` with the created request object, or `200 OK` with DAG plan if dry_run=true.

**Notes:** This endpoint wraps the logic currently in `cli.py:import_request()`. The import process (fetch from ReqMgr2, create DB records, plan DAG) runs synchronously. DAG submission and monitoring are handled by the lifecycle manager. For long-running imports, consider returning a job ID and polling for status.

#### 8.2.2 `GET /api/v1/activity/recent`

Recent system events across all requests.

**Query parameters:** `?limit=50`, `?event_type=`, `?request_name=`, `?since=<ISO timestamp>`

**Response:**
```json
{
  "events": [
    {
      "timestamp": "2026-03-01T14:32:00Z",
      "event_type": "status_transition",
      "request_name": "B2G-Run3...",
      "summary": "Round 1 started (12 work units)",
      "detail": {"from_status": "planning", "to_status": "active", "round": 1},
      "actor": null
    },
    {
      "timestamp": "2026-03-01T14:15:00Z",
      "event_type": "operator_action",
      "request_name": "TOP-Run3...",
      "summary": "Request stopped by operator",
      "detail": {"action": "stop", "reason": "site issues"},
      "actor": "user123"
    }
  ]
}
```

**Data source:** Built from `requests.status_transitions`, `dag_history`, and a new `operator_actions` audit log table.

#### 8.2.3 `GET /api/v1/activity` (full feed)

Same as `/activity/recent` but with pagination (`?offset=`, `?limit=`) and date range filtering (`?from=`, `?to=`).

#### 8.2.4 `GET /api/v1/dags/{id}/nodes`

List nodes in a DAG with their current status.

**Response:**
```json
{
  "nodes": [
    {
      "name": "proc_0_0",
      "role": "Processing",
      "status": "Done",
      "retries": 0,
      "site": "T2_US_MIT",
      "cluster_id": "12345.0",
      "duration_sec": 3600,
      "stdout_log": "/path/to/stdout",
      "stderr_log": "/path/to/stderr"
    }
  ]
}
```

**Data source:** Parse the DAGMan `.nodes.log` and `.dagman.out` files, plus `condor_q`/`condor_history` for running/completed jobs.

#### 8.2.5 `GET /api/v1/htcondor/queue`

Live HTCondor queue overview.

**Query parameters:** `?workflow_id=`, `?request_name=`, `?site=`

**Response:**
```json
{
  "summary": {
    "total": 150,
    "running": 45,
    "idle": 88,
    "held": 2
  },
  "by_workflow": [
    {
      "workflow_id": "uuid",
      "request_name": "B2G-Run3...",
      "running": 12,
      "idle": 30,
      "held": 0
    }
  ],
  "held_reasons": [
    {
      "cluster_id": "12345.0",
      "hold_reason": "Error from slot: SHADOW...",
      "site": "T2_US_MIT"
    }
  ]
}
```

**Data source:** Live `condor_q` query via htcondor2 Python bindings.

#### 8.2.6 `GET /api/v1/dags/{id}/logs`

DAGMan log file contents.

**Query parameters:** `?tail=100` (last N lines), `?file=dagman_out|nodes_log`

**Response:** Plain text log content.

### 8.3 API Error Handling

All API errors return JSON:
```json
{
  "detail": "Human-readable error message",
  "error_code": "MACHINE_READABLE_CODE"
}
```

The frontend JS translates these into user-visible toast notifications.

---

## 9. Error Handling and UX

### 9.1 Action Confirmation Dialogs

All mutating actions show a confirmation dialog before execution:

| Action    | Dialog content                                                  | Severity |
|-----------|-----------------------------------------------------------------|----------|
| Stop      | "Stop request {name}? Reason: [text input]"                    | Warning  |
| Release   | "Release held request {name}? It will resume processing."      | Info     |
| Fail      | "Permanently fail request {name}? This cannot be undone."      | Danger   |
| Restart   | "Restart {name} with incremented version? Old request will be superseded." | Warning |
| Ban site  | "Ban site {name}? Reason: [text], Duration: [dropdown], Scope: [all/workflow]" | Warning |
| Unban     | "Remove all active bans for site {name}?"                      | Info     |
| Import    | "Import {name} with the following settings: [summary]"         | Info     |

**Severity determines visual style:**
- **Info**: Blue confirmation button
- **Warning**: Yellow/orange confirmation button
- **Danger**: Red confirmation button, requires typing the request name to confirm

### 9.2 Toast Notifications

After every action:
- **Success**: Green toast, auto-dismiss after 5 seconds. "Request stopped successfully."
- **Error**: Red toast, persistent until dismissed. Shows error detail from API response.
- **Info**: Blue toast, auto-dismiss. "Data refreshed."

### 9.3 Loading States

- Initial page load: skeleton/placeholder content until first data fetch
- Auto-refresh: no loading indicator (seamless update)
- Manual refresh: spinner on the refresh button
- Action in progress: button shows spinner, disabled until complete

### 9.4 Offline/Error States

- If API is unreachable: banner "Cannot connect to WMS2 API" with retry button
- If individual endpoint fails: component shows "Failed to load" with retry
- Auto-refresh continues attempting; success clears error state

---

## 10. Visual Design

### 10.1 Design Principles

- **Clean and modern**: Minimal chrome, generous whitespace, clear typography
- **Information density**: Operators need to see a lot of data; don't hide it behind excessive clicks
- **Status at a glance**: Color-coded badges and progress bars for quick scanning
- **Dark mode**: Support both light and dark themes, defaulting to system preference

### 10.2 Color Palette

**Status colors (consistent across all views):**

| Status      | Color     | Usage                           |
|-------------|-----------|----------------------------------|
| Active/Running | Green (#22c55e) | Active requests, running nodes  |
| Queued/Idle    | Blue (#3b82f6)  | Waiting for resources           |
| Held/Warning   | Yellow (#eab308) | Needs attention                |
| Failed/Error   | Red (#ef4444)    | Failures, errors               |
| Completed      | Gray (#6b7280)   | Finished, no action needed     |
| Pilot          | Purple (#8b5cf6) | Pilot/probe phase              |
| Stopping       | Orange (#f97316) | In transition                  |

### 10.3 Layout

- **Navigation**: Left sidebar (collapsible) with page links: Dashboard, Requests, Sites, Import, Errors, Activity
- **Content area**: Full width, responsive (works on 1920px monitors and laptop screens)
- **Responsive**: Minimum supported width 1280px (this is a desktop operations tool, not mobile)

### 10.4 Typography

- System font stack: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`
- Monospace for: request names, dataset paths, cluster IDs, log output
- Font sizes: 14px base, 12px for dense tables, 16px for headings

---

## 11. Implementation Plan

### Phase 1: Foundation (MVP)

1. Set up FastAPI static file serving and Jinja2 templates
2. Create base template with navigation, auto-refresh controls
3. Dashboard page: status cards, request table, lifecycle indicator
4. Request list page with filtering and sorting
5. Request detail page (overview tab only)
6. API client JS module

**Deliverable:** Read-only dashboard that shows active requests and progress.

### Phase 2: Actions and Detail Views

7. Request actions: stop, release, fail, restart with confirmation dialogs
8. Workflow detail page with step metrics
9. DAG detail page with node counts and history timeline
10. Toast notification system
11. Implement `POST /api/v1/import` endpoint
12. Import request form page

**Deliverable:** Operators can monitor and manage requests from the browser.

### Phase 3: Charts and Sites

13. Chart.js integration — dashboard charts (status doughnut, events progress)
14. Per-workflow charts (events over time, node status over time, step metrics comparison)
15. Site management page with ban/unban actions
16. Site detail page
17. Implement `GET /api/v1/dags/{id}/nodes` endpoint
18. DAG node-level view

**Deliverable:** Rich visualizations and full site management.

### Phase 4: Activity Feed, HTCondor, Auth

19. Implement activity feed endpoints (`/activity/recent`, `/activity`)
20. Activity feed pages (dashboard widget + full page)
21. Errors/attention-needed page
22. Implement `GET /api/v1/htcondor/queue` endpoint
23. HTCondor queue integration on DAG detail page
24. Implement `GET /api/v1/dags/{id}/logs` endpoint
25. CERN SSO/IAM integration (authlib OIDC)
26. Role-based UI (Viewer vs Operator)
27. Dark mode toggle

**Deliverable:** Full-featured frontend with auth and live HTCondor data.

### Phase 5: Polish

28. Keyboard shortcuts (j/k navigation, r refresh, / search)
29. URL query parameter preservation (filters, sort, page)
30. Performance optimization (debounced search, pagination)
31. Error boundary handling
32. Documentation (user guide for operators)

---

## Appendix A: New Database Requirements

### A.1 Operator Actions Audit Log

A new table to record operator actions for the activity feed:

```sql
CREATE TABLE operator_actions (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor       TEXT NOT NULL,          -- username from SSO
    action      TEXT NOT NULL,          -- stop, release, fail, restart, ban, unban, import, update
    target_type TEXT NOT NULL,          -- request, site
    target_name TEXT NOT NULL,          -- request name or site name
    detail      JSONB,                 -- action-specific data (reason, old/new values, etc.)
    workflow_id UUID REFERENCES workflows(id)
);

CREATE INDEX idx_operator_actions_timestamp ON operator_actions (timestamp DESC);
CREATE INDEX idx_operator_actions_target ON operator_actions (target_type, target_name);
```

### A.2 Session Storage (if not using in-memory)

```sql
CREATE TABLE sessions (
    session_id  TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL,           -- viewer, operator
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    data        JSONB                   -- additional session data
);
```

---

## Appendix B: API Endpoints Not Yet Implemented (Needed for Frontend)

These endpoints are referenced by the frontend but do not yet exist in the WMS2 backend. They must be implemented before the corresponding frontend feature can work.

| Priority | Endpoint                       | Frontend feature          | Section |
|----------|--------------------------------|---------------------------|---------|
| P1       | `POST /api/v1/import`          | Import form               | 8.2.1   |
| P1       | `GET /api/v1/activity/recent`  | Dashboard activity feed   | 8.2.2   |
| P2       | `GET /api/v1/activity`         | Full activity page        | 8.2.3   |
| P2       | `GET /api/v1/dags/{id}/nodes`  | DAG node-level view       | 8.2.4   |
| P2       | `GET /api/v1/htcondor/queue`   | HTCondor queue overview   | 8.2.5   |
| P3       | `GET /api/v1/dags/{id}/logs`   | DAG log viewer            | 8.2.6   |
| P3       | `POST /sites/{name}/drain`     | Site drain action         | spec 7.1|
| P3       | `POST /sites/{name}/enable`    | Site enable action        | spec 7.1|
