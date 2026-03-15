"""Background monitoring data collector.

Periodically queries HTCondor and DB for monitoring data, storing
results in an in-memory cache so API endpoints return instantly.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from condora.db.repository import Repository

log = logging.getLogger(__name__)


class MonitoringCache:
    """In-memory cache for monitoring snapshots."""

    def __init__(self):
        self._htcondor_overview: dict | None = None
        self._updated_at: float = 0

    def set_overview(self, data: dict) -> None:
        self._htcondor_overview = data
        self._updated_at = time.time()

    def get_overview(self) -> tuple[dict | None, float]:
        """Return (data, age_seconds). Age is 0 if no data yet."""
        if self._htcondor_overview is None:
            return None, 0
        return self._htcondor_overview, time.time() - self._updated_at


async def collect_htcondor_overview(session_factory, condor) -> dict:
    """Collect HTCondor overview data for all active requests.

    Opens a fresh DB session, queries non-terminal requests, and for each
    with a running DAG queries HTCondor for per-site job breakdown.
    Returns the same structure as the htcondor_overview API endpoint.
    """
    async with session_factory() as session:
        repo = Repository(session)
        requests = await repo.get_non_terminal_requests()

        request_entries = []
        grand_totals = {"running": 0, "idle": 0, "held": 0}
        grand_by_site: dict[str, dict[str, int]] = {}
        htcondor_grand = {"running": 0, "idle": 0, "held": 0}

        for req_row in requests:
            wf = await repo.get_workflow_by_request(req_row.request_name)
            if not wf:
                continue

            # Collect all active DAGs for this workflow
            active_dags = await repo.get_active_dags_for_workflow(wf.id)
            if not active_dags:
                continue

            # Aggregate across all active DAGs
            agg_sites: dict[str, dict[str, int]] = {}
            agg_htcondor = {"running": 0, "idle": 0, "held": 0}
            agg_dag_nodes = {"total": 0, "done": 0, "running": 0,
                             "idle": 0, "failed": 0, "held": 0}
            agg_wus_done = 0
            agg_wus_total = 0
            round_numbers = []

            for dag in active_dags:
                if not dag.dagman_cluster_id:
                    continue

                round_num = getattr(dag, "round_number", None)
                if round_num is not None:
                    round_numbers.append(round_num)

                # Per-DAG HTCondor site query
                if condor:
                    try:
                        sites = await condor.query_dag_site_summary(
                            dag.dagman_cluster_id,
                            schedd_name=dag.schedd_name,
                        )
                    except Exception:
                        log.warning(
                            "HTCondor site query failed for DAG %s",
                            dag.id, exc_info=True,
                        )
                        sites = {}
                else:
                    sites = {}

                for site, counts in sites.items():
                    if site not in agg_sites:
                        agg_sites[site] = {"running": 0, "idle": 0, "held": 0}
                    for k in counts:
                        agg_sites[site][k] += counts.get(k, 0)
                        agg_htcondor[k] += counts.get(k, 0)

                agg_dag_nodes["total"] += dag.total_nodes or 0
                agg_dag_nodes["done"] += dag.nodes_done or 0
                agg_dag_nodes["running"] += dag.nodes_running or 0
                agg_dag_nodes["idle"] += getattr(dag, "nodes_idle", 0) or 0
                agg_dag_nodes["failed"] += dag.nodes_failed or 0
                agg_dag_nodes["held"] += getattr(dag, "nodes_held", 0) or 0

                completed_wus = dag.completed_work_units
                if isinstance(completed_wus, list):
                    agg_wus_done += len(completed_wus)
                else:
                    agg_wus_done += completed_wus or 0
                agg_wus_total += dag.total_work_units or 0

            # Build round display: "1-2" for multiple, "1" for single
            if round_numbers:
                round_numbers.sort()
                if len(round_numbers) == 1:
                    round_display = str(round_numbers[0])
                else:
                    round_display = f"{round_numbers[0]}-{round_numbers[-1]}"
            else:
                round_display = str(wf.current_round)

            totals = {
                "running": agg_htcondor["running"],
                "idle": agg_htcondor["idle"],
                "held": agg_htcondor["held"],
            }

            entry = {
                "request_name": req_row.request_name,
                "request_status": req_row.status,
                "round": round_display,
                "active_dags": len(active_dags),
                "dag_status": active_dags[-1].status if active_dags else None,
                "dagman_cluster_id": active_dags[-1].dagman_cluster_id if active_dags else None,
                "schedd_name": active_dags[-1].schedd_name if active_dags else None,
                "sites": agg_sites,
                "totals": totals,
                "dag_nodes": agg_dag_nodes,
                "work_units": {"done": agg_wus_done, "total": agg_wus_total},
                "events": {
                    "produced": wf.events_produced or 0,
                    "target": wf.target_events or 0,
                },
                "htcondor_totals": agg_htcondor,
            }
            request_entries.append(entry)

            for k in grand_totals:
                grand_totals[k] += totals[k]
            for k in htcondor_grand:
                htcondor_grand[k] += agg_htcondor[k]
            for site, counts in agg_sites.items():
                if site not in grand_by_site:
                    grand_by_site[site] = {"running": 0, "idle": 0, "held": 0}
                for k in counts:
                    grand_by_site[site][k] += counts[k]

    return {
        "requests": request_entries,
        "totals": {
            **grand_totals,
            "by_site": grand_by_site,
        },
        "htcondor_totals": htcondor_grand,
        "_collected_at": datetime.now(timezone.utc).isoformat(),
    }


async def run_monitoring_collector(
    session_factory, condor, settings, cache: MonitoringCache,
    on_cycle=None,
):
    """Background loop that periodically collects monitoring data.

    Runs every ``settings.lifecycle_cycle_interval`` seconds.
    Stores results in *cache*. Calls *on_cycle(exc)* after each
    attempt (exc is None on success).
    """
    interval = settings.lifecycle_cycle_interval
    log.info(
        "Monitoring collector started (interval=%ds)", interval,
    )
    while True:
        exc = None
        try:
            data = await collect_htcondor_overview(session_factory, condor)
            cache.set_overview(data)
            n_req = len(data["requests"])
            total_jobs = sum(data["htcondor_totals"].values())
            log.debug(
                "Monitoring collector: %d request(s), %d HTCondor jobs",
                n_req, total_jobs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            exc = e
            log.exception("Monitoring collector cycle failed")

        if on_cycle:
            on_cycle(exc)

        await asyncio.sleep(interval)
