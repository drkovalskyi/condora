import asyncio
import logging

import sqlalchemy
from fastapi import APIRouter, Depends, Request as FastAPIRequest
from fastapi.responses import PlainTextResponse

from wms2 import __version__
from wms2.db.repository import Repository

from .deps import get_repository

log = logging.getLogger(__name__)

router = APIRouter(tags=["monitoring"])


@router.get("/health")
async def health(raw_request: FastAPIRequest):
    checks = {}
    degraded = False

    # DB connectivity
    try:
        session_factory = getattr(raw_request.app.state, "session_factory", None)
        if session_factory:
            async with session_factory() as session:
                await asyncio.wait_for(
                    session.execute(sqlalchemy.text("SELECT 1")), timeout=5.0,
                )
            checks["database"] = "ok"
        else:
            checks["database"] = "unavailable"
            degraded = True
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        degraded = True

    # Background task liveness
    for task_name in ("lifecycle", "cric_sync", "monitoring_collector"):
        task = getattr(raw_request.app.state, f"{task_name}_task", None)
        if task is not None and not task.done():
            checks[task_name] = "running"
        else:
            checks[task_name] = "dead"
            degraded = True

    return {
        "status": "degraded" if degraded else "ok",
        "version": __version__,
        "checks": checks,
    }


@router.get("/status")
async def status(repo: Repository = Depends(get_repository)):
    counts = await repo.count_requests_by_status()
    return {"requests_by_status": counts}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(repo: Repository = Depends(get_repository)):
    counts = await repo.count_requests_by_status()
    lines = []
    lines.append("# HELP wms2_requests_total Number of requests by status")
    lines.append("# TYPE wms2_requests_total gauge")
    for status_val, count in counts.items():
        lines.append(f'wms2_requests_total{{status="{status_val}"}} {count}')
    return "\n".join(lines) + "\n"


@router.get("/htcondor-overview")
async def htcondor_overview(
    raw_request: FastAPIRequest,
    repo: Repository = Depends(get_repository),
):
    """HTCondor job counts grouped by request and site.

    Serves pre-collected data from the background monitoring collector.
    Falls back to a live query if the cache is empty (first cycle not
    yet completed after startup).
    """
    cache = getattr(raw_request.app.state, "monitoring_cache", None)
    if cache:
        data, age = cache.get_overview()
        if data is not None:
            return {**data, "_data_age_seconds": round(age, 1)}

    # Fallback: live query (only during startup before first collection)
    from wms2.core.monitoring_collector import collect_htcondor_overview

    session_factory = raw_request.app.state.session_factory
    condor = getattr(raw_request.app.state, "condor", None)
    data = await collect_htcondor_overview(session_factory, condor)
    return {**data, "_data_age_seconds": 0}
