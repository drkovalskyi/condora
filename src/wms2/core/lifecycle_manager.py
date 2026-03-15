import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone

from wms2.adapters.base import CondorAdapter
from wms2.config import Settings
from wms2.db.repository import Repository
from wms2.models.dag import wu_names
from wms2.models.enums import DAGStatus, RequestStatus, WorkflowStatus

logger = logging.getLogger(__name__)


def _aggregate_round_metrics(existing_metrics, dag, round_number,
                              wu_metrics_list=None):
    """Merge this round's DAG stats and WU performance data into step_metrics.

    When wu_metrics_list is provided (list of work_unit_metrics.json dicts),
    store them under a 'rounds' key keyed by round number. This preserves
    real per-step performance data that the adaptive algorithm needs.
    """
    prior = existing_metrics or {}
    result = {
        **prior,
        "rounds_completed": round_number + 1,
        "cumulative_nodes_done": prior.get("cumulative_nodes_done", 0) + (dag.nodes_done or 0),
        "cumulative_nodes_failed": prior.get("cumulative_nodes_failed", 0) + (dag.nodes_failed or 0),
        "last_round_nodes_done": dag.nodes_done or 0,
        "last_round_work_units": dag.total_work_units or 0,
    }

    # Store WU-level performance data for adaptive analysis
    if wu_metrics_list:
        rounds = prior.get("rounds", {})
        rounds[str(round_number)] = {
            "wu_metrics": wu_metrics_list,
            "nodes_done": dag.nodes_done or 0,
            "nodes_failed": dag.nodes_failed or 0,
            "work_units": dag.total_work_units or 0,
        }
        result["rounds"] = rounds

    return result


def _count_events_from_disk(submit_dir, completed_wus):
    """Read output events from work_unit_metrics.json on disk.

    Safety net for when events_produced is 0 in the DB despite work
    units having completed (session timing / commit ordering issue).
    """
    total = 0
    for wu_name in wu_names(completed_wus):
        metrics_path = os.path.join(submit_dir, wu_name, "work_unit_metrics.json")
        # In spool mode, merge output files end up at the spool root
        if not os.path.exists(metrics_path):
            metrics_path = os.path.join(submit_dir, "work_unit_metrics.json")
        if not os.path.exists(metrics_path):
            continue
        try:
            wu_metrics = json.load(open(metrics_path))
            per_step = wu_metrics.get("per_step", {})
            if not per_step:
                continue
            last_key = max(per_step.keys(), key=lambda k: int(k))
            last_step = per_step[last_key]
            ew = last_step.get("events_written")
            if ew and isinstance(ew, dict):
                total += ew.get("total", 0)
            else:
                ep = last_step.get("events_processed")
                if ep and isinstance(ep, dict):
                    total += ep.get("total", 0)
        except Exception:
            logger.warning("Failed to read WU metrics: %s", metrics_path)
    return total


_SKIP_EXTENSIONS = {".root", ".log", ".out", ".err"}


def _copy_dag_infrastructure(src_dir: str, dst_dir: str) -> None:
    """Copy DAG infrastructure files, skipping large outputs (.root/.log/.out/.err).

    Used for rescue DAG resubmission in spool mode where the original
    submit_dir is on a read-only sshfs mount.
    """
    for entry in os.scandir(src_dir):
        dst_path = os.path.join(dst_dir, entry.name)
        if entry.is_dir(follow_symlinks=False):
            if entry.name.startswith("mg_"):
                os.makedirs(dst_path, exist_ok=True)
                _copy_dag_infrastructure(entry.path, dst_path)
        elif entry.is_file(follow_symlinks=False):
            _, ext = os.path.splitext(entry.name)
            if ext not in _SKIP_EXTENSIONS:
                try:
                    shutil.copy2(entry.path, dst_path)
                except (PermissionError, OSError):
                    pass


def _compute_adaptive_params(config, dag, workflow, new_metrics, settings):
    """Run adaptive optimization if metrics are available.

    Returns adaptive_params dict or None if optimization can't run.
    """
    if not dag.submit_dir or not dag.completed_work_units:
        return None

    original_nthreads = int(config.get("multicore", 0))
    if original_nthreads <= 0:
        return None

    try:
        from wms2.core.adaptive import compute_round_optimization
    except ImportError:
        logger.warning("wms2.core.adaptive not available — skipping optimization")
        return None

    params = workflow.splitting_params or {}
    events_per_job = (params.get("events_per_job")
                      or params.get("eventsPerJob") or 0)

    # ── Compute historical peak RSS across all previous rounds ──
    # Spec requires: "Peak value is tracked for all rounds, not only the latest"
    historical_peak_rss_mb = 0.0
    sm = workflow.step_metrics or {}
    for _rk, rd in (sm.get("rounds") or {}).items():
        for wu in rd.get("wu_metrics") or []:
            for _sn, sd in (wu.get("per_step") or {}).items():
                prss = sd.get("peak_rss_mb")
                if isinstance(prss, dict):
                    val = prss.get("max", 0) or 0
                elif isinstance(prss, (int, float)):
                    val = prss
                else:
                    val = 0
                if val > historical_peak_rss_mb:
                    historical_peak_rss_mb = val

    # ── Resolve target wall time (per-request override > global setting) ──
    target_wt_hours = config.get("target_wall_time_hours",
                                 settings.target_wall_time_hours)
    target_wall_time_sec = target_wt_hours * 3600 if target_wt_hours > 0 else 0.0

    current_round = getattr(workflow, "current_round", 0) or 0

    # ── Collect inline WU metrics from enriched completed_work_units ──
    # In spool mode the schedd cleans up proc_*_metrics.json before we
    # get here.  The DAG monitor captured the data into the DB at WU
    # completion time — pass it through so the optimizer doesn't depend
    # on disk files.
    inline_wu_metrics = []
    for item in (dag.completed_work_units or []):
        if isinstance(item, dict) and item.get("metrics"):
            m = item["metrics"]
            if m.get("num_proc_jobs", 0) > 0:
                inline_wu_metrics.append(m)

    try:
        return compute_round_optimization(
            submit_dir=dag.submit_dir,
            completed_wus=wu_names(dag.completed_work_units),
            original_nthreads=original_nthreads,
            request_cpus=original_nthreads,
            default_memory_per_core=settings.default_memory_per_core,
            max_memory_per_core=settings.max_memory_per_core,
            safety_margin=settings.safety_margin,
            events_per_job=events_per_job,
            jobs_per_wu=settings.jobs_per_work_unit,
            min_request_cpus=settings.min_request_cpus,
            historical_peak_rss_mb=historical_peak_rss_mb,
            split_tmpfs=config.get("split_tmpfs", False),
            target_wall_time_sec=target_wall_time_sec,
            current_round=current_round,
            inline_wu_metrics=inline_wu_metrics or None,
        )
    except Exception:
        logger.exception("Adaptive optimization failed — using defaults")
        return None


async def complete_round(repo, settings, workflow, dag):
    """Shared round-completion logic for CLI and lifecycle manager.

    Reads WU metrics from disk, counts output events, aggregates step_metrics,
    runs adaptive optimization, advances the round offset, and updates the
    workflow in the DB.

    Returns a dict with:
      - new_round: int
      - events_from_wus: int
      - step_metrics: dict (enriched)
      - adaptive_params: dict | None
      - proc_jobs: int
    """
    config = workflow.config_data or {}
    is_gen = config.get("_is_gen", False)
    params = workflow.splitting_params or {}
    # DD-21: Use the DAG's round_number (set at planning time) rather than
    # workflow.current_round, which is now advanced by the planner before
    # the DAG completes.
    current_round = getattr(dag, "round_number", None)
    if current_round is None:
        current_round = getattr(workflow, "current_round", 0) or 0

    # ── Count proc jobs ──
    node_counts = dag.node_counts or {}
    proc_jobs = node_counts.get("processing", dag.nodes_done)

    # ── Pilot throwaway check ──
    pilot_throwaway = (
        current_round == 0
        and config.get("pilot_throwaway", settings.pilot_throwaway)
    )

    # ── events_produced fix: read from disk if DB shows 0 ──
    completed_wus = dag.completed_work_units or []
    events_from_wus = 0
    if is_gen and not pilot_throwaway and (workflow.events_produced or 0) == 0 and completed_wus:
        events_from_wus = _count_events_from_disk(dag.submit_dir, completed_wus)
        if events_from_wus > 0:
            logger.info(
                "Workflow %s: events_produced=0 in DB but %d from disk — fixing",
                workflow.request_name, events_from_wus,
            )
            await repo.update_workflow(
                workflow.id, events_produced=events_from_wus
            )

    # ── Collect WU metrics from DB (enriched completed_work_units) ──
    wu_metrics_list = []
    for item in completed_wus:
        if isinstance(item, dict) and item.get("metrics"):
            wu_metrics_list.append(item["metrics"])

    # Fall back to disk if no metrics in DB (old format or pre-enrichment data)
    if not wu_metrics_list and dag.submit_dir:
        seen_metrics_paths: set[str] = set()
        for wu_name in wu_names(completed_wus):
            metrics_path = os.path.join(
                dag.submit_dir, wu_name, "work_unit_metrics.json"
            )
            if not os.path.exists(metrics_path):
                metrics_path = os.path.join(
                    dag.submit_dir, "work_unit_metrics.json"
                )
            real_path = os.path.realpath(metrics_path)
            if real_path in seen_metrics_paths:
                continue
            seen_metrics_paths.add(real_path)
            if os.path.exists(metrics_path):
                try:
                    wu_metrics_list.append(json.load(open(metrics_path)))
                except Exception:
                    logger.warning("Failed to read WU metrics: %s", metrics_path)

    # ── Aggregate step_metrics ──
    new_metrics = _aggregate_round_metrics(
        workflow.step_metrics, dag, current_round,
        wu_metrics_list=wu_metrics_list or None,
    )

    # ── Store resource params used for this round ──
    # Round 0 uses original request params; round 1+ uses previous adaptive
    prev_ap = (workflow.step_metrics or {}).get("adaptive_params")
    if current_round == 0 or not prev_ap:
        resource_params = {
            "memory_mb": int(config.get("memory_mb", 0)),
            "nthreads": int(config.get("multicore", 0)),
        }
    else:
        resource_params = {
            "memory_mb": prev_ap.get("tuned_memory_mb",
                                     int(config.get("memory_mb", 0))),
            "nthreads": prev_ap.get("tuned_nthreads",
                                    int(config.get("multicore", 0))),
        }
    round_key = str(current_round)
    if round_key in new_metrics.get("rounds", {}):
        new_metrics["rounds"][round_key]["resource_params"] = resource_params
        # Store effective events_per_job for this round — needed by
        # _compute_jobs_per_wu_from_write_mb to scale pilot write_mb to
        # production events_per_job.
        node_counts_rc = dag.node_counts or {}
        effective_epj = node_counts_rc.get("effective_events_per_job") or \
            (params.get("events_per_job") or params.get("eventsPerJob") or 100_000)
        new_metrics["rounds"][round_key]["effective_events_per_job"] = int(effective_epj)
        eff_frac = node_counts_rc.get("effective_fraction")
        if eff_frac is not None:
            new_metrics["rounds"][round_key]["effective_fraction"] = float(eff_frac)

    # ── Adaptive optimization ──
    adaptive_params = _compute_adaptive_params(
        config, dag, workflow, new_metrics, settings
    )
    if adaptive_params:
        new_metrics["adaptive_params"] = adaptive_params
        # Also store per-round for history
        if round_key in new_metrics.get("rounds", {}):
            new_metrics["rounds"][round_key]["adaptive_params"] = adaptive_params
        logger.info(
            "Workflow %s: adaptive optimization — cpus=%d, memory=%d MB [%s], "
            "job_multiplier=%d, cpu_eff=%.1f%%",
            workflow.request_name,
            adaptive_params.get("tuned_request_cpus",
                                adaptive_params.get("tuned_nthreads", 0)),
            adaptive_params.get("tuned_memory_mb", 0),
            adaptive_params.get("memory_source", "?"),
            adaptive_params.get("job_multiplier", 1),
            (adaptive_params.get("metrics_summary", {}) or {}).get(
                "weighted_cpu_eff", 0
            ) * 100,
        )
        tp_opt = adaptive_params.get("throughput_optimization")
        if isinstance(tp_opt, dict):
            logger.info(
                "Workflow %s: throughput optimization — "
                "epj=%d, measured=%.2f s/ev, estimated_wall=%.1fh",
                workflow.request_name,
                tp_opt.get("tuned_events_per_job", 0),
                tp_opt.get("measured_wall_per_event_sec", 0),
                tp_opt.get("estimated_total_wall_sec", 0) / 3600,
            )

    # ── Record round completion ──
    # DD-21: Offsets and current_round are now advanced at plan time
    # (in plan_production_dag), not at completion time.  We only store
    # the enriched step_metrics here.
    new_round = current_round + 1
    update_kwargs = {
        "step_metrics": new_metrics,
    }

    # Safety net: if current_round wasn't advanced at plan time (e.g.
    # DAG was planned with old code before DD-21), ensure it's at least
    # dag.round_number + 1 so the next round gets the correct number.
    wf_current = getattr(workflow, "current_round", 0) or 0
    if wf_current < new_round:
        update_kwargs["current_round"] = new_round

    await repo.update_workflow(workflow.id, **update_kwargs)

    return {
        "new_round": new_round,
        "events_from_wus": events_from_wus,
        "step_metrics": new_metrics,
        "adaptive_params": adaptive_params,
        "proc_jobs": proc_jobs,
        "pilot_throwaway": pilot_throwaway,
    }


class RequestLifecycleManager:
    """
    Single owner of the request state machine. Runs a continuous loop that
    evaluates all non-terminal requests and dispatches work to the appropriate
    component workers.

    Accepts a session_factory and creates a fresh DB session per cycle so that:
    - Each cycle sees the latest DB state (including API-injected requests)
    - Changes are committed after each request evaluation
    - Crashes don't lose previously committed state

    Adapter instances (condor, reqmgr, dbs, rucio, cric) are long-lived and
    shared across cycles. Worker components (DAGMonitor, DAGPlanner, etc.)
    are rebuilt per-cycle with the fresh repository.
    """

    def __init__(
        self,
        session_factory_or_repo=None,
        condor_adapter: CondorAdapter = None,
        settings: Settings = None,
        *,
        # Service mode: pass adapters, workers built per-cycle
        reqmgr=None,
        dbs=None,
        rucio=None,
        cric=None,
        # Test/legacy mode: pass pre-built workers directly
        repository=None,
        workflow_manager=None,
        dag_planner=None,
        dag_monitor=None,
        output_manager=None,
        error_handler=None,
        # Optional per-cycle callback: called with None on success, exc on failure
        on_cycle=None,
    ):
        self.condor = condor_adapter
        self.settings = settings

        # Resolve: keyword `repository=` forces repo mode; otherwise check
        # if the first positional arg is a session_factory (async_sessionmaker)
        # or a repository-like object (has get_request method).
        if repository is not None:
            # Explicit keyword: always repo mode
            self.session_factory = None
            self.db = repository
        elif session_factory_or_repo is not None and hasattr(
            session_factory_or_repo, "get_request"
        ):
            # Repository-like (real Repository or MagicMock with get_request)
            self.session_factory = None
            self.db = session_factory_or_repo
        else:
            # Service mode: session_factory (async_sessionmaker) passed
            self.session_factory = session_factory_or_repo
            self.db = None

        self.reqmgr = reqmgr
        self.dbs = dbs
        self.rucio = rucio
        self.cric = cric
        self.on_cycle = on_cycle
        self.workflow_manager = workflow_manager
        self.dag_planner = dag_planner
        self.dag_monitor = dag_monitor
        self.output_manager = output_manager
        self.error_handler = error_handler

        self.status_timeouts = {
            RequestStatus.SUBMITTED: settings.timeout_submitted,
            RequestStatus.QUEUED: settings.timeout_queued,
            RequestStatus.PILOT_RUNNING: settings.timeout_pilot_running,
            RequestStatus.PLANNING: settings.timeout_planning,
            RequestStatus.ACTIVE: settings.timeout_active,
            RequestStatus.STOPPING: settings.timeout_stopping,
            RequestStatus.RESUBMITTING: settings.timeout_resubmitting,
            RequestStatus.PAUSED: settings.timeout_active,
        }

        self._dispatch = {
            RequestStatus.SUBMITTED: self._handle_submitted,
            RequestStatus.QUEUED: self._handle_queued,
            RequestStatus.PILOT_RUNNING: self._handle_pilot_running,
            RequestStatus.ACTIVE: self._handle_active,
            RequestStatus.STOPPING: self._handle_stopping,
            RequestStatus.RESUBMITTING: self._handle_resubmitting,
            RequestStatus.PAUSED: self._handle_paused,
            RequestStatus.HELD: self._handle_held,
            RequestStatus.PARTIAL: self._handle_partial,
        }

    def _build_workers(self, repo: Repository):
        """Build per-cycle worker components with a fresh repository."""
        from wms2.core.dag_monitor import DAGMonitor
        from wms2.core.dag_planner import DAGPlanner
        from wms2.core.error_handler import ErrorHandler
        from wms2.core.output_manager import OutputManager
        from wms2.core.site_manager import SiteManager
        from wms2.core.workflow_manager import WorkflowManager

        sm = SiteManager(repo, self.settings, cric_adapter=self.cric,
                         rucio_adapter=self.rucio)
        self.db = repo
        self.workflow_manager = WorkflowManager(repo, self.reqmgr) if self.reqmgr else None
        self.dag_planner = DAGPlanner(
            repo, self.dbs, self.rucio, self.condor, self.settings, site_manager=sm,
        )
        self.dag_monitor = DAGMonitor(repo, self.condor, settings=self.settings)
        from wms2.adapters.mock import MockDBSAdapter
        self.output_manager = OutputManager(repo, MockDBSAdapter(), self.rucio,
                                            site_manager=sm)
        self.error_handler = ErrorHandler(repo, self.condor, self.settings, site_manager=sm)

    # ── Main Loop ───────────────────────────────────────────────

    async def main_loop(self):
        """Main loop: create fresh session per cycle, evaluate all requests."""
        logger.info("Lifecycle manager main_loop started")
        while True:
            cycle_exc = None
            try:
                logger.debug("Lifecycle cycle starting")
                async with self.session_factory() as session:
                    repo = Repository(session)
                    self._build_workers(repo)

                    requests = await repo.get_non_terminal_requests()
                    logger.info(
                        "Lifecycle cycle: %d non-terminal request(s)", len(requests),
                    )
                    failed_names: set[str] = set()
                    while requests:
                        request = requests.pop(0)
                        req_name = request.request_name
                        try:
                            await self.evaluate_request(request)
                            await session.commit()
                        except Exception:
                            logger.exception("Error evaluating %s", req_name)
                            await session.rollback()
                            failed_names.add(req_name)
                            # After rollback, ORM objects are expired — re-fetch
                            # remaining requests so the loop can continue.
                            requests = [
                                r for r in await repo.get_non_terminal_requests()
                                if r.request_name not in failed_names
                            ]

                await asyncio.sleep(self.settings.lifecycle_cycle_interval)
            except asyncio.CancelledError:
                logger.info("Lifecycle manager shutting down")
                break
            except Exception as exc:
                cycle_exc = exc
                logger.exception("Lifecycle manager cycle error")
                await asyncio.sleep(self.settings.lifecycle_cycle_interval)

            if self.on_cycle:
                try:
                    self.on_cycle(cycle_exc)
                except Exception:
                    logger.debug("on_cycle callback error", exc_info=True)

    async def evaluate_request(self, request):
        """Match on current status, dispatch to the appropriate handler."""
        status = RequestStatus(request.status)

        if self._is_stuck(request):
            await self._handle_stuck(request)
            return

        handler = self._dispatch.get(status)
        if handler:
            await handler(request)

    # ── State Handlers ──────────────────────────────────────────

    async def _handle_submitted(self, request):
        """Validate request and move to admission queue."""
        if self.workflow_manager is None:
            logger.debug("Skipping _handle_submitted: workflow_manager not available")
            return
        # Skip if a workflow already exists (CLI may have created one concurrently)
        existing = await self.db.get_workflow_by_request(request.request_name)
        if existing:
            logger.info("Workflow already exists for %s — skipping import",
                        request.request_name)
            # If a DAG is already submitted/running, go straight to ACTIVE
            if existing.dag_id:
                dag = await self.db.get_dag(existing.dag_id)
                if dag and dag.status in (DAGStatus.SUBMITTED.value,
                                          DAGStatus.RUNNING.value):
                    await self.transition(request, RequestStatus.ACTIVE)
                    return
        else:
            await self.workflow_manager.import_request(request.request_name)
        await self.transition(request, RequestStatus.QUEUED)

    async def _handle_queued(self, request):
        """Check admission capacity and start pilot or planning."""
        if self.dag_planner is None:
            logger.debug("Skipping _handle_queued: dag_planner not available")
            return

        active_count = await self.db.count_active_dags()
        if active_count >= self.settings.max_active_dags:
            return

        next_pending = await self.db.get_queued_requests(limit=1)
        if next_pending and next_pending[0].request_name != request.request_name:
            return

        workflow = await self.db.get_workflow_by_request(request.request_name)
        if not workflow:
            return

        # If there are already active DAGs for this workflow, transition
        # directly to ACTIVE — _handle_active will manage them.
        active_dags = await self.db.get_active_dags_for_workflow(workflow.id)
        if active_dags:
            await self.transition(request, RequestStatus.ACTIVE)
            return

        # Check existing DAG state before planning a new one
        if workflow.dag_id:
            dag = await self.db.get_dag(workflow.dag_id)
            if dag:
                # DAG already submitted/running (CLI race) — go to ACTIVE
                if dag.status in (DAGStatus.SUBMITTED.value,
                                  DAGStatus.RUNNING.value):
                    await self.transition(request, RequestStatus.ACTIVE)
                    return
                # Rescue DAG re-admission — submit the *original* DAG file;
                # DAGMan's AutoRescue finds and applies the rescue file automatically.
                if dag.rescue_dag_path and dag.status == DAGStatus.READY.value:
                    config = (workflow.config_data or {})
                    condor_pool = config.get("condor_pool", "local")
                    use_spool = (
                        condor_pool == "global"
                        and bool(self.settings.spool_mount)
                        and bool(self.settings.remote_spool_prefix)
                    )
                    from wms2.core.schedd_selector import select_schedd
                    schedd_name = (
                        select_schedd(self.settings.schedd_pool, self.settings.remote_schedd)
                        if use_spool else None
                    )

                    # If spool dir was cleaned up (e.g. old DAG removed),
                    # skip rescue and fall through to fresh replanning.
                    if use_spool and dag.submit_dir and not os.path.isdir(dag.submit_dir):
                        logger.warning(
                            "Rescue spool dir missing (%s) — fresh replan",
                            dag.submit_dir,
                        )
                        await self.db.update_dag(dag.id, status=DAGStatus.FAILED.value)
                        # Fall through to plan_production_dag below
                    else:
                        dag_file = dag.dag_file_path
                        local_copy_dir = None
                        if use_spool:
                            # Spool-mode DAG: submit_dir is on sshfs mount
                            # (read-only). Copy only DAG infrastructure
                            # files to local temp so from_dag() can write
                            # .condor.sub, then re-spool. Skip large output
                            # files (.root, .log, .out, .err, metrics).
                            local_copy_dir = tempfile.mkdtemp(
                                prefix="wms2_rescue_"
                            )
                            await asyncio.to_thread(
                                _copy_dag_infrastructure,
                                dag.submit_dir, local_copy_dir,
                            )
                            dag_file = os.path.join(
                                local_copy_dir,
                                os.path.basename(dag.dag_file_path),
                            )

                        # Remove stale .condor.sub and .lock so from_dag()
                        # can recreate them without Force. We must NOT use
                        # Force because that passes -force to DAGMan which
                        # causes it to ignore the rescue file.
                        for stale in (dag_file + ".condor.sub",
                                      dag_file + ".lock"):
                            if os.path.exists(stale):
                                os.remove(stale)

                        try:
                            cluster_id, schedd = await self.condor.submit_dag(
                                dag_file, force=False,
                                spool=use_spool, schedd_name=schedd_name,
                            )
                        finally:
                            if local_copy_dir:
                                shutil.rmtree(local_copy_dir, ignore_errors=True)

                        # Map new spool Iwd to local mount for monitoring
                        new_submit_dir = dag.submit_dir
                        if use_spool:
                            iwd = await self.condor.get_job_iwd(
                                cluster_id, schedd
                            )
                            if iwd:
                                new_submit_dir = iwd.replace(
                                    self.settings.remote_spool_prefix,
                                    self.settings.spool_mount, 1,
                                )

                        await self.db.update_dag(
                            dag.id,
                            dagman_cluster_id=cluster_id,
                            schedd_name=schedd,
                            status=DAGStatus.SUBMITTED.value,
                            submit_dir=new_submit_dir,
                        )
                        # Reset workflow status back to active (was set to
                        # RESUBMITTING by error_handler._prepare_rescue)
                        await self.db.update_workflow(
                            workflow.id,
                            status=WorkflowStatus.ACTIVE.value,
                        )
                        await self.transition(request, RequestStatus.ACTIVE)
                        return

        current_round = getattr(workflow, "current_round", 0) or 0
        is_adaptive = getattr(request, "adaptive", False)

        # Round 0 IS the pilot — no separate pilot submission.
        # All rounds use plan_production_dag; round 0 just uses
        # smaller test_fraction sizing.
        if current_round > 0:
            # Round 2+: always adaptive
            dag = await self.dag_planner.plan_production_dag(workflow, adaptive=True)
        else:
            dag = await self.dag_planner.plan_production_dag(
                workflow, adaptive=is_adaptive,
            )
        if dag is None:
            # All work done — update workflow status before completing request
            if workflow:
                await self.db.update_workflow(
                    workflow.id, status=WorkflowStatus.COMPLETED.value,
                )
            await self.transition(request, RequestStatus.COMPLETED)
        else:
            await self.transition(request, RequestStatus.ACTIVE)

    async def _handle_pilot_running(self, request):
        """Poll pilot status, trigger DAG planning on completion."""
        if self.dag_planner is None:
            logger.debug("Skipping _handle_pilot_running: dag_planner not available")
            return

        workflow = await self.db.get_workflow_by_request(request.request_name)
        if not workflow or not workflow.pilot_cluster_id:
            return

        try:
            completed = await self.condor.check_job_completed(
                workflow.pilot_cluster_id, workflow.pilot_schedd
            )
        except Exception:
            logger.warning(
                "Cannot reach schedd for pilot %s — will retry next cycle",
                request.request_name,
            )
            return
        if completed:
            is_adaptive = getattr(request, "adaptive", False)
            report_path = os.path.join(
                workflow.pilot_output_path or "", "pilot_metrics.json"
            )
            if os.path.exists(report_path):
                await self.dag_planner.handle_pilot_completion(
                    workflow, report_path, adaptive=is_adaptive,
                )
            else:
                # No pilot metrics — plan with defaults
                await self.dag_planner.plan_production_dag(
                    workflow, adaptive=is_adaptive,
                )
            await self.transition(request, RequestStatus.ACTIVE)

    async def _handle_active(self, request):
        """Poll all active DAGs, process outputs, check round advance."""
        if self.dag_monitor is None:
            logger.debug("Skipping _handle_active: dag_monitor not available")
            return

        workflow = await self.db.get_workflow_by_request(request.request_name)
        if not workflow:
            return

        # Fetch all active DAGs for this workflow
        active_dags = await self.db.get_active_dags_for_workflow(workflow.id)

        # Also check the head DAG (dag_id) if no active DAGs — it may
        # have completed or failed since last cycle.
        if not active_dags and workflow.dag_id:
            dag = await self.db.get_dag(workflow.dag_id)
            if dag and dag.status == DAGStatus.COMPLETED.value:
                # Guard: only process round completion once — check if
                # this DAG's round is already in step_metrics.rounds.
                sm = workflow.step_metrics or {}
                dag_round = getattr(dag, "round_number", None)
                already_recorded = (
                    dag_round is not None
                    and str(dag_round) in (sm.get("rounds") or {})
                )
                if not already_recorded:
                    await self._cleanup_condor_dag(dag)
                    await self._handle_dag_round_completion(request, workflow, dag)
                # Fall through to aggregate counts and plan next round
            elif dag and dag.status in (DAGStatus.PARTIAL.value, DAGStatus.FAILED.value):
                await self._handle_single_dag_failure(request, workflow, dag)
                return
            # Don't return — fall through to aggregate counts and
            # potentially plan the next round below.

        # Poll each active DAG
        for dag in active_dags:
            result = await self.dag_monitor.poll_dag(dag)

            # Process completed work units through output manager
            if result.newly_completed_work_units:
                await self._process_completed_wus(request, workflow, result)

            # Handle per-DAG terminal states
            if result.status == DAGStatus.COMPLETED:
                dag = await self.db.get_dag(dag.id)  # re-fetch after poll
                await self._cleanup_condor_dag(dag)
                await self._handle_dag_round_completion(request, workflow, dag)
                # Re-fetch request — may have transitioned
                request = await self.db.get_request(request.request_name)
                if not request or request.status != RequestStatus.ACTIVE.value:
                    return
                workflow = await self.db.get_workflow_by_request(request.request_name)
                if not workflow:
                    return
                continue

            elif result.status in (DAGStatus.PARTIAL, DAGStatus.FAILED):
                await self._handle_single_dag_failure(request, workflow, dag)
                # Re-fetch — failure handler may have transitioned
                request = await self.db.get_request(request.request_name)
                if not request or request.status != RequestStatus.ACTIVE.value:
                    return
                workflow = await self.db.get_workflow_by_request(request.request_name)
                if not workflow:
                    return
                continue

            # Per-DAG: site exclusion, tail escalation (running DAGs only)
            dag = await self.db.get_dag(dag.id)
            if dag and dag.status in (DAGStatus.SUBMITTED.value, DAGStatus.RUNNING.value):
                await self._check_mid_dag_site_exclusions(dag, workflow)
                await self._maybe_escalate_tail_priority(dag, workflow, result)

        # Every cycle: retry failed Rucio calls
        if self.output_manager:
            from wms2.core.output_manager import RucioRegistrationError
            try:
                await self.output_manager.process_blocks_for_workflow(
                    workflow.id, config_data=workflow.config_data,
                )
            except RucioRegistrationError as e:
                logger.error(
                    "Rucio retry failed for %s — holding request: %s",
                    request.request_name, e,
                )
                await self.transition(request, RequestStatus.HELD)
                return
            except Exception:
                logger.warning(
                    "Output block processing failed for %s — will retry next cycle",
                    request.request_name, exc_info=True,
                )

        # Aggregate workflow-level node counts across all active DAGs (B9)
        await self._aggregate_workflow_counts(workflow)

        # Check if all work is done and all DAGs terminal
        request = await self.db.get_request(request.request_name)
        if not request or request.status != RequestStatus.ACTIVE.value:
            return
        workflow = await self.db.get_workflow_by_request(request.request_name)
        if not workflow:
            return
        remaining_active = await self.db.get_active_dags_for_workflow(workflow.id)
        if not remaining_active and self._target_reached(workflow):
            await self.transition(request, RequestStatus.COMPLETED)
            return

        # Check pipelined round advance conditions
        if remaining_active:
            await self._maybe_advance_round(request, workflow)
        elif self.dag_planner and not self._target_reached(workflow):
            # No active DAGs, target not reached — plan next round directly.
            # This handles the case where the last DAG completed but the
            # pipelining path kept the request ACTIVE (max_concurrent_rounds>1).
            try:
                workflow = await self.db.get_workflow_by_request(request.request_name)
                if workflow:
                    new_dag = await self.dag_planner.plan_production_dag(
                        workflow, adaptive=True,
                    )
                    if new_dag:
                        logger.info(
                            "Request %s: planned next round — DAG %s (round %d)",
                            request.request_name, new_dag.id,
                            getattr(new_dag, "round_number", "?"),
                        )
            except Exception:
                logger.warning(
                    "Round advance failed for %s — will retry next cycle",
                    request.request_name, exc_info=True,
                )

    async def _process_completed_wus(self, request, workflow, result):
        """Process completed work units: output registration + event counting."""
        if not result.newly_completed_work_units:
            return

        # Output registration
        if self.output_manager:
            from wms2.core.output_manager import RucioRegistrationError
            try:
                all_blocks = await self.db.get_processing_blocks(workflow.id)
                latest_by_ds: dict[str, object] = {}
                for b in all_blocks:
                    prev = latest_by_ds.get(b.dataset_name)
                    if prev is None or b.created_at > prev.created_at:
                        latest_by_ds[b.dataset_name] = b
                blocks = list(latest_by_ds.values())

                for wu in result.newly_completed_work_units:
                    manifest = wu.get("manifest") or {}
                    datasets_info = manifest.get("datasets", {})
                    for block in blocks:
                        ds_info = datasets_info.get(block.dataset_name, {})
                        await self.output_manager.handle_work_unit_completion(
                            workflow.id, block.id, {
                                "output_files": ds_info.get("files", []),
                                "site": manifest.get("site", "local"),
                                "node_name": wu["group_name"],
                            },
                            config_data=workflow.config_data,
                        )
            except RucioRegistrationError as e:
                logger.error(
                    "Rucio registration failed for %s — holding request: %s",
                    request.request_name, e,
                )
                await self.transition(request, RequestStatus.HELD)
                return
            except Exception:
                logger.warning(
                    "Output registration failed for %s — will retry next cycle",
                    request.request_name, exc_info=True,
                )

        # Accumulate production counters (skip pilot_throwaway)
        wf = await self.db.get_workflow_by_request(request.request_name)
        wf_config = (wf.config_data or {}) if wf else {}
        is_pilot_throwaway = (
            wf_config.get("pilot_throwaway", self.settings.pilot_throwaway)
            and not any(
                (isinstance(item, dict) and item.get("metrics"))
                or isinstance(item, str)
                for item in (wf.completed_work_units if hasattr(wf, 'completed_work_units') else [])
            )
        )
        # Check round 0 via the DAG's round_number to determine pilot_throwaway
        # for the specific DAG that produced these WUs
        dag_round = None
        if result.newly_completed_work_units:
            # The DAG ID is in the result
            dag_id = result.dag_id
            if dag_id:
                try:
                    from uuid import UUID
                    dag_obj = await self.db.get_dag(UUID(dag_id) if isinstance(dag_id, str) else dag_id)
                    if dag_obj:
                        dag_round = getattr(dag_obj, "round_number", None)
                except Exception:
                    pass

        is_pilot_throwaway = (
            dag_round == 0
            and wf_config.get("pilot_throwaway", self.settings.pilot_throwaway)
        ) if dag_round is not None else False

        if not is_pilot_throwaway:
            total_new_events = sum(
                wu.get("output_events", 0) for wu in result.newly_completed_work_units
            )
            if total_new_events > 0 and wf:
                await self.db.update_workflow(
                    wf.id,
                    events_produced=(wf.events_produced or 0) + total_new_events,
                )

    async def _handle_single_dag_failure(self, request, workflow, dag):
        """Handle a single DAG that completed with failures."""
        if self.error_handler:
            completion = await self.error_handler.handle_dag_completion(
                dag, request, workflow
            )
            if completion.action == "rescue":
                if completion.problem_sites:
                    from wms2.core.error_handler import ErrorHandler
                    ErrorHandler.apply_site_exclusions(
                        dag.submit_dir, completion.problem_sites
                    )
                await self.transition(request, RequestStatus.RESUBMITTING)
                return
        # No error_handler or completion.action == "hold"
        await self.transition(request, RequestStatus.HELD)

    async def _aggregate_workflow_counts(self, workflow):
        """Aggregate node counts from all active DAGs into workflow."""
        active_dags = await self.db.get_active_dags_for_workflow(workflow.id)
        total_nodes = 0
        total_done = 0
        total_failed = 0
        total_running = 0
        total_idle = 0
        for d in active_dags:
            total_nodes += d.total_nodes or 0
            total_done += d.nodes_done or 0
            total_failed += d.nodes_failed or 0
            total_running += d.nodes_running or 0
            total_idle += d.nodes_idle or 0
        await self.db.update_workflow(
            workflow.id,
            total_nodes=total_nodes,
            nodes_done=total_done,
            nodes_failed=total_failed,
            nodes_running=total_running,
            nodes_queued=total_idle,
        )

    @staticmethod
    def _target_reached(workflow) -> bool:
        """Check if the production target has been met.

        Returns True when either:
        - GEN: events_produced >= target_events (target > 0)
        - File-based: files_processed >= total_input_files (total > 0)
        - No target set (target == 0): True (single-round / non-adaptive)
        """
        config = workflow.config_data or {}
        is_gen = config.get("_is_gen", False)
        if is_gen:
            target = workflow.target_events or 0
            produced = workflow.events_produced or 0
            return target == 0 or produced >= target
        else:
            total_files = workflow.total_input_files or 0
            processed = workflow.files_processed or 0
            return total_files == 0 or processed >= total_files

    async def _maybe_advance_round(self, request, workflow):
        """Check round advance conditions and plan next round if met (DD-20)."""
        config = workflow.config_data or {}

        # 1. Don't start new rounds if target reached
        if self._target_reached(workflow):
            return

        # 2. Check max_concurrent_rounds
        active_dags = await self.db.get_active_dags_for_workflow(workflow.id)
        if len(active_dags) >= self.settings.max_concurrent_rounds:
            return

        # 3. Check completion fraction on latest (most recently created) DAG
        if not active_dags:
            return
        latest_dag = max(active_dags, key=lambda d: d.created_at)
        total_wus = latest_dag.total_work_units or 0
        if total_wus == 0:
            return
        done_wus = len(wu_names(latest_dag.completed_work_units))
        if done_wus / total_wus < self.settings.round_advance_completion_fraction:
            return

        # 4. Check running fraction across all active DAGs
        total_running = sum(d.nodes_running or 0 for d in active_dags)
        total_schedulable = sum(
            (d.nodes_running or 0) + (d.nodes_idle or 0) for d in active_dags
        )
        if total_schedulable > 0:
            if total_running / total_schedulable < self.settings.round_advance_running_fraction:
                return

        # All conditions met — plan next round
        if not self.dag_planner:
            return

        workflow = await self.db.get_workflow_by_request(request.request_name)
        if not workflow:
            return

        try:
            new_dag = await self.dag_planner.plan_production_dag(
                workflow, adaptive=True,
            )
            if new_dag:
                logger.info(
                    "Request %s: pipelined round advance — new DAG %s (round %d)",
                    request.request_name, new_dag.id,
                    getattr(new_dag, "round_number", "?"),
                )
        except Exception:
            logger.warning(
                "Pipelined round advance failed for %s — will retry next cycle",
                request.request_name, exc_info=True,
            )

    async def _handle_stopping(self, request):
        """Monitor clean stop progress. Remove ALL active DAGs.

        If a DAG's stop_reason starts with "FAIL:", transition to FAILED
        instead of PAUSED (operator requested fail via /fail endpoint).
        """
        workflow = await self.db.get_workflow_by_request(request.request_name)
        if not workflow:
            return

        # Collect all active DAGs + the head DAG
        active_dags = await self.db.get_active_dags_for_workflow(workflow.id)
        head_dag = None
        if workflow.dag_id:
            head_dag = await self.db.get_dag(workflow.dag_id)
            if head_dag and head_dag not in active_dags:
                # Head DAG may already be terminal — only add if still active
                if head_dag.status in (DAGStatus.SUBMITTED.value, DAGStatus.RUNNING.value):
                    active_dags.append(head_dag)

        if not active_dags and not head_dag:
            return

        all_stopped = True
        fail_requested = False
        for dag in active_dags:
            dagman_status = await self.condor.query_job(
                schedd_name=dag.schedd_name, cluster_id=dag.dagman_cluster_id
            )
            if dagman_status is None:
                # DAGMan process gone
                if (dag.stop_reason or "").startswith("FAIL:"):
                    fail_requested = True
            else:
                all_stopped = False
                # DAGMan still alive — retry condor_rm
                try:
                    await self.condor.remove_job(
                        schedd_name=dag.schedd_name,
                        cluster_id=dag.dagman_cluster_id,
                    )
                except Exception:
                    logger.warning(
                        "Retry condor_rm failed for DAG %s (%s)",
                        dag.dagman_cluster_id, request.request_name,
                        exc_info=True,
                    )

        if not all_stopped:
            return

        # All DAGs stopped
        if fail_requested or (head_dag and (head_dag.stop_reason or "").startswith("FAIL:")):
            for d in await self.db.list_dags(workflow_id=workflow.id):
                if d.status not in (DAGStatus.FAILED.value, DAGStatus.COMPLETED.value):
                    await self.db.update_dag(d.id, status=DAGStatus.FAILED.value)
            for block in await self.db.get_processing_blocks(workflow.id):
                if block.status == "open":
                    await self.db.update_processing_block(block.id, status="failed")
            await self.db.update_workflow(workflow.id, status="failed")
            await self.transition(request, RequestStatus.FAILED)
            logger.info("Request %s: stopping -> failed (operator-initiated)", request.request_name)
        else:
            for d in active_dags:
                await self.db.update_dag(d.id, status=DAGStatus.STOPPED.value)
            await self.transition(request, RequestStatus.PAUSED)

    async def _handle_resubmitting(self, request):
        """Prepare recovery DAG if needed, then move to admission queue.

        Two paths reach RESUBMITTING:
        1. Error handler rescue — rescue DAG record already created (READY).
        2. Clean stop resume (PAUSED → RESUBMITTING) — DAG is STOPPED,
           needs _prepare_recovery to create rescue DAG record.
        """
        workflow = await self.db.get_workflow_by_request(request.request_name)
        if workflow and workflow.dag_id:
            dag = await self.db.get_dag(workflow.dag_id)
            if dag and dag.status == DAGStatus.STOPPED.value:
                # Clean stop resume — create rescue DAG record
                await self._prepare_recovery(request, workflow, dag)
                return
        # Error handler path or fallback — rescue DAG already exists
        await self.transition(request, RequestStatus.QUEUED)

    async def _handle_paused(self, request):
        """PAUSED: operator-initiated clean stop. Waiting for Resume. No-op."""
        pass

    async def _handle_held(self, request):
        """HELD: stable state waiting for operator action. No-op."""
        pass

    async def _handle_partial(self, request):
        """Handle partial DAG completion — re-evaluation on subsequent cycles.

        Legacy state kept for backward compatibility with existing DB rows.
        New transitions use HELD instead. No-op.
        """
        pass

    # ── Operator Actions ─────────────────────────────────────────

    async def release_held_request(self, request_name: str):
        """Release a HELD request back to the admission queue.

        The next lifecycle cycle will handle it: rescue DAG if one exists,
        or a new round.
        """
        request = await self.db.get_request(request_name)
        if not request:
            raise ValueError(f"Request {request_name} not found")
        if request.status != RequestStatus.HELD.value:
            raise ValueError(
                f"Cannot release request in {request.status} state; must be held"
            )
        await self.transition(request, RequestStatus.QUEUED)

    async def fail_request(self, request_name: str):
        """Fail a HELD, PARTIAL, or PAUSED request: kill running DAG, mark
        DAGs/blocks as failed, transition request to FAILED."""
        request = await self.db.get_request(request_name)
        if not request:
            raise ValueError(f"Request {request_name} not found")
        allowed = (RequestStatus.HELD.value, RequestStatus.PARTIAL.value,
                   RequestStatus.PAUSED.value)
        if request.status not in allowed:
            raise ValueError(
                f"Cannot fail request in {request.status} state; "
                f"must be held or partial"
            )

        workflow = await self.db.get_workflow_by_request(request_name)
        if workflow:
            # 1. condor_rm on ALL active DAGs (swallow errors)
            active_dags = await self.db.get_active_dags_for_workflow(workflow.id)
            for dag in active_dags:
                try:
                    await self.condor.remove_job(
                        schedd_name=dag.schedd_name,
                        cluster_id=dag.dagman_cluster_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to remove DAG %s for %s",
                        dag.id, request_name,
                    )

            # 2. Mark all non-terminal DAGs as FAILED
            for dag in await self.db.list_dags(workflow_id=workflow.id):
                if dag.status not in (
                    DAGStatus.FAILED.value, DAGStatus.COMPLETED.value
                ):
                    await self.db.update_dag(dag.id, status=DAGStatus.FAILED.value)

            # 3. Mark open processing blocks as "failed"
            for block in await self.db.get_processing_blocks(workflow.id):
                if block.status == "open":
                    await self.db.update_processing_block(
                        block.id, status="failed"
                    )

        # 4. Transition to FAILED
        logger.info(
            "Operator-initiated fail for %s (output invalidation deferred)",
            request_name,
        )
        await self.transition(request, RequestStatus.FAILED)

    async def restart_request(self, request_name: str) -> str:
        """Kill+clone: create new request with incremented processing_version,
        fail the old one. Returns the new request name."""
        request = await self.db.get_request(request_name)
        if not request:
            raise ValueError(f"Request {request_name} not found")
        allowed = (
            RequestStatus.HELD.value, RequestStatus.PARTIAL.value,
            RequestStatus.ABORTED.value, RequestStatus.FAILED.value,
            RequestStatus.PAUSED.value,
        )
        if request.status not in allowed:
            raise ValueError(
                f"Cannot restart request in {request.status} state; "
                f"must be held, partial, paused, aborted, or failed"
            )

        # 1. Compute new version
        request_data = request.request_data or {}
        current_version = request_data.get("processing_version", 1)
        new_version = current_version + 1
        new_name = f"{request_name}_v{new_version}"

        # 2. Clone request with incremented version
        new_data = {**request_data, "processing_version": new_version}
        now = datetime.now(timezone.utc)
        await self.db.create_request(
            request_name=new_name,
            requestor=request.requestor,
            requestor_dn=request.requestor_dn,
            request_data=new_data,
            payload_config=request.payload_config,
            splitting_params=request.splitting_params,
            input_dataset=request.input_dataset,
            campaign=request.campaign,
            priority=request.priority,
            urgent=request.urgent,
            adaptive=request.adaptive,
            production_steps=request.production_steps or [],
            previous_version_request=request_name,
            cleanup_policy=request.cleanup_policy,
            status=RequestStatus.SUBMITTED.value,
            status_transitions=[],
            created_at=now,
            updated_at=now,
        )

        # 3. Link old → new
        await self.db.update_request(
            request_name, superseded_by_request=new_name
        )

        # 4. Fail old request (condor_rm, mark DAGs/blocks, → FAILED)
        #    Skip if already terminal (aborted/failed)
        terminal = (RequestStatus.ABORTED.value, RequestStatus.FAILED.value)
        if request.status not in terminal:
            await self.fail_request(request_name)

        logger.info(
            "Restarted %s → %s (processing_version=%d)",
            request_name, new_name, new_version,
        )
        return new_name

    async def get_error_summary(self, request_name: str) -> dict:
        """Read-only error inspection. Aggregates POST data from the
        current DAG's submit directory.

        Uses its own DB session to avoid sharing the lifecycle cycle's
        session with concurrent API requests (which caused connection leaks).
        """
        from wms2.core.error_handler import ErrorHandler

        async with self.session_factory() as session:
            repo = Repository(session)

            request = await repo.get_request(request_name)
            if not request:
                raise ValueError(f"Request {request_name} not found")

            result = {
                "request_name": request_name,
                "status": request.status,
                "dag_id": None,
                "nodes_done": 0,
                "nodes_failed": 0,
                "total_nodes": 0,
                "error_summary": {},
                "site_summary": {},
                "bad_input_files": [],
            }

            workflow = await repo.get_workflow_by_request(request_name)
            if not workflow or not workflow.dag_id:
                return result

            dag = await repo.get_dag(workflow.dag_id)
            if not dag:
                return result

            result["dag_id"] = str(dag.id)
            result["nodes_done"] = dag.nodes_done or 0
            result["nodes_failed"] = dag.nodes_failed or 0
            result["total_nodes"] = dag.total_nodes or 0

            if not dag.submit_dir:
                return result

        # Filesystem reads outside the session — run in thread pool to avoid
        # blocking the async event loop (these files are on sshfs in spool mode)
        eh = ErrorHandler(None, self.condor, self.settings)
        loop = asyncio.get_running_loop()
        post_data = await loop.run_in_executor(
            None, eh.read_post_data, dag.submit_dir
        )
        if not post_data:
            return result

        # Aggregate by category
        category_counts = {}
        site_counts = {}
        bad_files = set()
        for entry in post_data:
            cat = entry.get("classification", {}).get("category", "unknown")
            category_counts[cat] = category_counts.get(cat, 0) + 1

            site = entry.get("job", {}).get("site", "unknown")
            sc = site_counts.setdefault(site, {"total": 0, "failed": 0})
            sc["total"] += 1
            if cat != "success":
                sc["failed"] += 1

            bad_file = entry.get("classification", {}).get("bad_input_file")
            if bad_file:
                bad_files.add(bad_file)

        result["error_summary"] = category_counts
        result["site_summary"] = site_counts
        result["bad_input_files"] = sorted(bad_files)
        return result

    # ── Proactive Mid-DAG Site Exclusion ─────────────────────

    async def _check_mid_dag_site_exclusions(self, dag, workflow):
        """Detect failing sites during a running DAG and exclude them from
        idle landing jobs so new WUs don't land at broken sites.

        Reads post.json files from already-failed WUs, identifies problem
        sites via ErrorHandler.analyze_site_failures(), then uses condor_edit
        to add Requirements exclusions on idle landing jobs.
        """
        if not dag.submit_dir or not dag.dagman_cluster_id:
            return

        nc = dag.node_counts or {}
        # Check if we already ran exclusion analysis for the current set of failures
        wus_failed = nc.get("wus_failed", 0)
        if wus_failed < 1:
            return
        last_exclusion_at = nc.get("_mid_dag_exclusion_wus_failed", 0)
        if wus_failed <= last_exclusion_at:
            return  # no new failures since last check

        already_excluded = nc.get("_mid_dag_excluded_sites", [])

        # Read post.json files from filesystem (may be on sshfs — run in thread pool)
        from wms2.core.error_handler import ErrorHandler
        eh = ErrorHandler(None, self.condor, self.settings)
        loop = asyncio.get_running_loop()
        try:
            post_data = await loop.run_in_executor(
                None, eh.read_post_data, dag.submit_dir
            )
        except Exception:
            logger.debug(
                "Mid-DAG exclusion: failed to read post data for %s",
                workflow.request_name, exc_info=True,
            )
            return

        if not post_data:
            return

        problem_sites = eh.analyze_site_failures(post_data)
        # Only consider newly detected sites
        new_sites = [s for s in problem_sites if s not in already_excluded]

        # Record that we checked at this failure count (even if no new sites)
        nc["_mid_dag_exclusion_wus_failed"] = wus_failed
        if not new_sites:
            await self.db.update_dag(dag.id, node_counts=nc)
            return

        # Find idle landing jobs and add exclusion Requirements
        try:
            landing_jobs = await self.condor.query_dag_landing_jobs(
                dag.dagman_cluster_id,
                schedd_name=dag.schedd_name,
            )
        except Exception:
            logger.debug(
                "Mid-DAG exclusion: failed to query landing jobs for %s",
                workflow.request_name, exc_info=True,
            )
            return

        if not landing_jobs:
            logger.debug(
                "Mid-DAG exclusion: no idle landing jobs for %s",
                workflow.request_name,
            )
            nc["_mid_dag_excluded_sites"] = already_excluded + new_sites
            await self.db.update_dag(dag.id, node_counts=nc)
            return

        # Build exclusion clause for all problem sites (existing + new)
        all_excluded = already_excluded + new_sites
        exclusion_clauses = " && ".join(
            f'TARGET.GLIDEIN_CMSSite =!= "{site}"' for site in all_excluded
        )
        # Also remove excluded sites from DESIRED_Sites
        excluded_set = set(all_excluded)

        edited_count = 0
        for job in landing_jobs:
            cid = job["cluster_id"]
            pid = job["proc_id"]
            try:
                constraint = f"ClusterId == {cid} && ProcId == {pid}"
                await self.condor.edit_job_attr(
                    constraint,
                    "Requirements",
                    f"({exclusion_clauses}) && ({job['requirements']})"
                    if job.get("requirements") else exclusion_clauses,
                    schedd_name=dag.schedd_name,
                )
                edited_count += 1
            except Exception:
                logger.debug(
                    "Mid-DAG exclusion: failed to edit landing job %d.%d",
                    cid, pid,
                )

        nc["_mid_dag_excluded_sites"] = all_excluded
        await self.db.update_dag(dag.id, node_counts=nc)

        logger.info(
            "Mid-DAG site exclusion: excluded %s from %d idle landing jobs "
            "for %s (DAG %s)",
            new_sites, edited_count, workflow.request_name,
            dag.dagman_cluster_id,
        )

    # ── Tail WU Priority Escalation ──────────────────────────

    async def _maybe_escalate_tail_priority(self, dag, workflow, poll_result):
        """Bump remaining job priorities when most WUs are done.

        When ≥90% (configurable) of work units have completed, the last few
        WUs become the bottleneck for round completion. Escalating their
        priority via condor_qedit helps them schedule faster.
        """
        threshold = self.settings.tail_escalation_threshold
        total_wus = dag.total_work_units or 0
        if total_wus < 2:
            return  # nothing to escalate with 0-1 WUs

        # Re-fetch DAG to get updated node_counts from poll_dag()
        dag = await self.db.get_dag(dag.id)
        if not dag:
            return
        nc = dag.node_counts or {}
        wus_done = nc.get("wus_done", 0)

        if wus_done < total_wus * threshold:
            return  # not at tail yet

        # Check if already escalated (stored in node_counts to avoid repeated edits)
        if nc.get("_tail_escalated"):
            return

        # Escalate: bump priority of all remaining jobs under this DAGMan
        boost = self.settings.tail_escalation_priority
        if not dag.dagman_cluster_id:
            return

        try:
            # Edit all non-completed payload jobs under this DAG hierarchy.
            # Two-level DAG: outer DAGMan's children are inner sub-DAGMans
            # (JobUniverse == 7), payload jobs sit under those.
            count = await self.condor.edit_dag_payload_attr(
                dag.dagman_cluster_id, "Priority",
                str(boost),
                schedd_name=dag.schedd_name,
            )
            logger.info(
                "Tail escalation: bumped priority to %d for %d payload jobs "
                "in DAG %s (%d/%d WUs done) [%s]",
                boost, count, dag.dagman_cluster_id, wus_done, total_wus,
                workflow.request_name,
            )
            # Mark escalation so we don't repeat
            nc["_tail_escalated"] = True
            await self.db.update_dag(dag.id, node_counts=nc)
        except Exception:
            logger.warning(
                "Tail escalation failed for DAG %s",
                dag.dagman_cluster_id, exc_info=True,
            )

    # ── Adaptive Round Completion ──────────────────────────────

    async def _handle_dag_round_completion(self, request, workflow, dag):
        """Handle completion of one DAG (one round). Delegates shared logic
        to the module-level complete_round(), then checks whether more
        rounds are needed.  With pipelining, the request stays ACTIVE —
        _maybe_advance_round() handles starting new rounds.
        """
        # Re-fetch workflow to get the latest state
        workflow = await self.db.get_workflow_by_request(request.request_name)

        # Shared logic: metrics, adaptive
        result = await complete_round(self.db, self.settings, workflow, dag)
        new_round = result["new_round"]

        # Pilot throwaway: clean up output files (best-effort)
        if result.get("pilot_throwaway"):
            await self._cleanup_pilot_output(workflow, dag)

        # Re-fetch workflow to get latest production counters
        workflow = await self.db.get_workflow_by_request(request.request_name)
        config = workflow.config_data or {}
        is_gen = config.get("_is_gen", False)

        # Apply production_steps priority demotion between rounds
        steps = request.production_steps or []
        if steps and is_gen:
            target = workflow.target_events or 0
            produced = workflow.events_produced or 0
            if target > 0:
                progress = produced / target
                step = steps[0]
                fraction = step["fraction"] if isinstance(step, dict) else step.fraction
                if progress >= fraction:
                    priority = step["priority"] if isinstance(step, dict) else step.priority
                    remaining_steps = steps[1:]
                    await self.db.update_request(
                        request.request_name,
                        priority=priority,
                        production_steps=[
                            s if isinstance(s, dict)
                            else {"fraction": s.fraction, "priority": s.priority}
                            for s in remaining_steps
                        ],
                    )

        # Check if target reached AND no more active DAGs → COMPLETED
        remaining_active = await self.db.get_active_dags_for_workflow(workflow.id)
        if not remaining_active and self._target_reached(workflow):
            logger.info(
                "Request %s: COMPLETED — target reached, all DAGs terminal",
                request.request_name,
            )
            await self.transition(request, RequestStatus.COMPLETED)
            return

        # With pipelining: request stays ACTIVE.  _maybe_advance_round()
        # in _handle_active() will plan the next round when conditions are met.
        # For backward compat (max_concurrent_rounds=1): if no active DAGs and
        # target not reached, transition to QUEUED so _handle_queued plans next.
        if not remaining_active and self.settings.max_concurrent_rounds <= 1:
            logger.info(
                "Request %s: round %d complete, advancing to round %d "
                "(produced=%s, target=%s)",
                request.request_name, dag.round_number, new_round,
                workflow.events_produced if is_gen else workflow.files_processed,
                workflow.target_events if is_gen else workflow.total_input_files,
            )
            await self.transition(request, RequestStatus.QUEUED)
            return

        logger.info(
            "Request %s: DAG round %d complete (produced=%s, target=%s), "
            "%d active DAGs remain",
            request.request_name, dag.round_number,
            workflow.events_produced if is_gen else workflow.files_processed,
            workflow.target_events if is_gen else workflow.total_input_files,
            len(remaining_active),
        )

    # ── Pilot Output Cleanup ─────────────────────────────────────

    async def _cleanup_pilot_output(self, workflow, dag):
        """Delete merged output files from a throwaway pilot round (best-effort).

        Reads merge_output.json from each completed WU to find merged file paths,
        then deletes them via os.remove (local) or gfal-rm (grid).
        """
        config = workflow.config_data or {}
        stageout_mode = config.get("stageout_mode", self.settings.stageout_mode)
        completed_wus = dag.completed_work_units or []
        if not completed_wus:
            return

        deleted = 0
        for wu_name in wu_names(completed_wus):
            # Find merge_output.json
            merge_path = os.path.join(dag.submit_dir, wu_name, "merge_output.json")
            if not os.path.exists(merge_path):
                merge_path = os.path.join(dag.submit_dir, "merge_output.json")
            if not os.path.exists(merge_path):
                continue

            try:
                with open(merge_path) as f:
                    merge_output = json.load(f)
            except Exception:
                logger.debug("Could not read merge_output: %s", merge_path)
                continue

            # merge_output has "merged_files": [{"pfn": "...", "lfn": "..."}]
            for mf in merge_output.get("merged_files", []):
                pfn = mf.get("pfn", "")
                lfn = mf.get("lfn", "")
                try:
                    if stageout_mode in ("local", "local-grid"):
                        local_path = pfn or os.path.join(
                            self.settings.local_pfn_prefix, lfn.lstrip("/")
                        )
                        if os.path.exists(local_path):
                            os.remove(local_path)
                            deleted += 1
                    else:
                        # Grid: use gfal-rm
                        if pfn:
                            proc = await asyncio.subprocess.create_subprocess_exec(
                                "gfal-rm", pfn,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            await proc.wait()
                            deleted += 1
                except Exception:
                    logger.debug("Pilot cleanup failed for %s", pfn or lfn, exc_info=True)

        if deleted:
            logger.info("Pilot throwaway: deleted %d merged output files for %s",
                        deleted, workflow.request_name)

    # ── Condor Cleanup ─────────────────────────────────────────

    async def _cleanup_condor_dag(self, dag):
        """Remove a finished DAGMan job from the schedd queue (best-effort).

        Completed DAGMan jobs (status=4) linger on the schedd until
        MAX_HISTORY_ROTATIONS removes them.  On a shared remote schedd
        this can take days, so we proactively clean up.
        """
        if not dag or not dag.dagman_cluster_id:
            return
        try:
            await self.condor.remove_job(
                schedd_name=dag.schedd_name,
                cluster_id=dag.dagman_cluster_id,
            )
            logger.debug("Removed finished DAG %s (cluster %s) from %s",
                         dag.id, dag.dagman_cluster_id, dag.schedd_name)
        except Exception:
            # Already gone or auth issue — not critical
            logger.debug("Could not remove DAG %s from schedd (may already be gone)",
                         dag.dagman_cluster_id)

    # ── Clean Stop ──────────────────────────────────────────────

    async def initiate_clean_stop(self, request_name: str, reason: str):
        request = await self.db.get_request(request_name)
        if not request:
            return
        workflow = await self.db.get_workflow_by_request(request_name)
        if not workflow:
            return

        now = datetime.now(timezone.utc)

        # PILOT_RUNNING: remove pilot job, no DAG to stop
        if request.status == RequestStatus.PILOT_RUNNING.value and workflow.pilot_cluster_id:
            await self.condor.remove_job(
                schedd_name=workflow.pilot_schedd, cluster_id=workflow.pilot_cluster_id
            )
            await self.db.update_workflow(workflow.id, status=WorkflowStatus.STOPPING.value)
            await self.transition(request, RequestStatus.STOPPING)
            return

        # ACTIVE: remove ALL active DAGs
        active_dags = await self.db.get_active_dags_for_workflow(workflow.id)
        if not active_dags:
            # Fall back to head DAG
            if workflow.dag_id:
                dag = await self.db.get_dag(workflow.dag_id)
                if dag and dag.status in (DAGStatus.SUBMITTED.value, DAGStatus.RUNNING.value):
                    active_dags = [dag]
        if not active_dags:
            return

        for dag in active_dags:
            await self.condor.remove_job(
                schedd_name=dag.schedd_name, cluster_id=dag.dagman_cluster_id
            )
            await self.db.update_dag(dag.id, stop_requested_at=now, stop_reason=reason)
        await self.db.update_workflow(workflow.id, status=WorkflowStatus.STOPPING.value)
        await self.transition(request, RequestStatus.STOPPING)

    async def _prepare_recovery(self, request, workflow, dag):
        """After clean stop, create recovery DAG record and transition."""
        rescue_path = f"{dag.dag_file_path}.rescue001"
        new_dag = await self.db.create_dag(
            workflow_id=workflow.id,
            dag_file_path=dag.dag_file_path,
            submit_dir=dag.submit_dir,
            rescue_dag_path=rescue_path,
            parent_dag_id=dag.id,
            total_nodes=dag.total_nodes,
            total_edges=dag.total_edges,
            node_counts=dag.node_counts,
            total_work_units=dag.total_work_units,
            completed_work_units=dag.completed_work_units,
            status=DAGStatus.READY.value,
        )
        await self.db.update_workflow(
            workflow.id, dag_id=new_dag.id, status="resubmitting"
        )

        # Partial production: consume step, demote priority
        steps = request.production_steps or []
        if steps:
            step = steps[0]
            remaining = steps[1:]
            await self.db.update_request(
                request.request_name,
                priority=step["priority"] if isinstance(step, dict) else step.priority,
                production_steps=[
                    s if isinstance(s, dict) else {"fraction": s.fraction, "priority": s.priority}
                    for s in remaining
                ],
            )

        await self.transition(request, RequestStatus.QUEUED)

    # ── Timeout Detection ───────────────────────────────────────

    def _is_stuck(self, request) -> bool:
        status = RequestStatus(request.status)
        timeout = self.status_timeouts.get(status)
        if timeout is None:
            return False
        elapsed = (datetime.now(timezone.utc) - request.updated_at).total_seconds()
        return elapsed > timeout

    async def _handle_stuck(self, request):
        status = RequestStatus(request.status)
        elapsed = datetime.now(timezone.utc) - request.updated_at
        logger.warning(
            "Request %s stuck in %s for %.0fs",
            request.request_name, status.value, elapsed.total_seconds(),
        )

        if status in (
            RequestStatus.SUBMITTED,
            RequestStatus.PLANNING,
            RequestStatus.RESUBMITTING,
        ):
            await self.transition(request, RequestStatus.FAILED)

        elif status == RequestStatus.QUEUED:
            logger.error(
                "ALERT [stuck_in_queue] %s: queued for %.1f days",
                request.request_name, elapsed.total_seconds() / 86400,
            )

        elif status == RequestStatus.PILOT_RUNNING:
            workflow = await self.db.get_workflow_by_request(request.request_name)
            if workflow and workflow.pilot_cluster_id:
                job_exists = await self.condor.query_job(
                    schedd_name=workflow.pilot_schedd,
                    cluster_id=workflow.pilot_cluster_id,
                )
                if job_exists is None:
                    await self.transition(request, RequestStatus.FAILED)

        elif status == RequestStatus.ACTIVE:
            workflow = await self.db.get_workflow_by_request(request.request_name)
            if workflow and workflow.dag_id:
                dag = await self.db.get_dag(workflow.dag_id)
                if dag:
                    reachable = await self.condor.ping_schedd(dag.schedd_name)
                    if reachable:
                        logger.error(
                            "ALERT [slow_dag] %s: running for %.1f days",
                            request.request_name, elapsed.total_seconds() / 86400,
                        )
                    else:
                        logger.error(
                            "ALERT [schedd_unreachable] %s: schedd %s unreachable",
                            request.request_name, dag.schedd_name,
                        )

        elif status == RequestStatus.STOPPING:
            await self.initiate_clean_stop(
                request.request_name, reason="retry after stuck stop"
            )

    # ── State Transition ────────────────────────────────────────

    async def transition(self, request, new_status: RequestStatus):
        """Record a state transition with timestamp."""
        now = datetime.now(timezone.utc)
        old_status = request.status if isinstance(request.status, str) else request.status.value
        old_transitions = request.status_transitions or []
        new_transition = {
            "from": old_status,
            "to": new_status.value,
            "timestamp": now.isoformat(),
        }
        await self.db.update_request(
            request.request_name,
            status=new_status.value,
            status_transitions=old_transitions + [new_transition],
            updated_at=now,
        )
        logger.info(
            "Request %s: %s -> %s",
            request.request_name, old_status, new_status.value,
        )
