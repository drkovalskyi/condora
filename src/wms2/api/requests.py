import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request as FastAPIRequest
from pydantic import BaseModel

from wms2.db.repository import Repository
from wms2.models.enums import DAGStatus, RequestStatus
from wms2.models.request import Request, RequestCreate, RequestUpdate

from .deps import get_repository, get_settings

logger = logging.getLogger(__name__)


class StopRequest(BaseModel):
    reason: str = "Operator-initiated clean stop"


class PriorityProfileUpdate(BaseModel):
    high: int
    nominal: int
    switch_fraction: float

router = APIRouter(prefix="/requests", tags=["requests"])


@router.post("", response_model=Request, status_code=201)
async def create_request(
    body: RequestCreate,
    repo: Repository = Depends(get_repository),
):
    existing = await repo.get_request(body.request_name)
    if existing:
        raise HTTPException(status_code=409, detail="Request already exists")

    now = datetime.now(timezone.utc)
    request_data = body.model_dump(
        exclude={"request_name", "requestor", "requestor_dn", "campaign",
                 "priority", "urgent", "production_steps", "payload_config",
                 "input_dataset", "splitting_params", "cleanup_policy"}
    )
    row = await repo.create_request(
        request_name=body.request_name,
        requestor=body.requestor,
        requestor_dn=body.requestor_dn,
        request_data=request_data,
        payload_config=body.payload_config,
        splitting_params=body.splitting_params,
        input_dataset=body.input_dataset,
        campaign=body.campaign,
        priority=body.priority,
        urgent=body.urgent,
        production_steps=[s.model_dump() for s in body.production_steps],
        status=RequestStatus.NEW.value,
        status_transitions=[],
        cleanup_policy=body.cleanup_policy.value,
        created_at=now,
        updated_at=now,
    )
    return Request.model_validate(row)


@router.get("")
async def list_requests(
    status: str | None = None,
    campaign: str | None = None,
    limit: int = 100,
    offset: int = 0,
    repo: Repository = Depends(get_repository),
):
    rows = await repo.list_requests_with_progress(
        status=status, campaign=campaign, limit=limit, offset=offset,
    )
    result = []
    for item in rows:
        req = Request.model_validate(item["request"])
        d = req.model_dump(mode="json")
        ep = item["events_produced"] or 0
        te = item["target_events"] or 0
        d["events_produced"] = ep
        d["target_events"] = te
        d["progress_pct"] = (ep / te * 100) if te > 0 else None
        cd = item.get("config_data") or {}
        d["condor_pool"] = cd.get("condor_pool", "local")
        d["stageout_mode"] = cd.get("stageout_mode", "local")
        result.append(d)
    return result


@router.get("/{request_name}", response_model=Request)
async def get_request(
    request_name: str,
    repo: Repository = Depends(get_repository),
):
    row = await repo.get_request(request_name)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return Request.model_validate(row)


@router.patch("/{request_name}", response_model=Request)
async def update_request(
    request_name: str,
    body: RequestUpdate,
    repo: Repository = Depends(get_repository),
):
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    updates = body.model_dump(exclude_none=True)
    if "production_steps" in updates:
        updates["production_steps"] = [s.model_dump() for s in body.production_steps]

    if not updates:
        return Request.model_validate(existing)

    row = await repo.update_request(request_name, **updates)
    return Request.model_validate(row)


@router.delete("/{request_name}", response_model=Request)
async def abort_request(
    request_name: str,
    repo: Repository = Depends(get_repository),
):
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    terminal = (RequestStatus.COMPLETED.value, RequestStatus.FAILED.value, RequestStatus.ABORTED.value)
    if existing.status in terminal:
        raise HTTPException(status_code=400, detail=f"Cannot abort request in {existing.status} state")

    now = datetime.now(timezone.utc)
    old_transitions = existing.status_transitions or []
    new_transition = {
        "from": existing.status,
        "to": RequestStatus.ABORTED.value,
        "timestamp": now.isoformat(),
    }
    row = await repo.update_request(
        request_name,
        status=RequestStatus.ABORTED.value,
        status_transitions=old_transitions + [new_transition],
        updated_at=now,
    )
    return Request.model_validate(row)


@router.post("/{request_name}/stop", response_model=dict)
async def stop_request(
    request_name: str,
    body: StopRequest = StopRequest(),
    raw_request: FastAPIRequest = None,
    repo: Repository = Depends(get_repository),
):
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    stoppable = (RequestStatus.ACTIVE.value, RequestStatus.PILOT_RUNNING.value)
    if existing.status not in stoppable:
        raise HTTPException(
            status_code=400,
            detail=f"Can only stop requests in active or pilot_running state, "
                   f"current status: {existing.status}",
        )

    previous_status = existing.status

    # Mark current DAG as stop-requested and remove from condor
    workflow = await repo.get_workflow_by_request(request_name)
    if workflow and workflow.dag_id:
        now = datetime.now(timezone.utc)
        dag = await repo.get_dag(workflow.dag_id)
        if dag and dag.status in (DAGStatus.SUBMITTED.value, DAGStatus.RUNNING.value):
            try:
                condor = raw_request.app.state.condor
                await condor.remove_job(
                    schedd_name=dag.schedd_name,
                    cluster_id=dag.dagman_cluster_id,
                )
            except Exception:
                logger.warning(
                    "Failed to condor_rm DAG %s for %s — lifecycle manager will retry",
                    dag.dagman_cluster_id, request_name, exc_info=True,
                )
        await repo.update_dag(
            workflow.dag_id,
            stop_requested_at=now,
            stop_reason=body.reason,
        )

    # Transition to STOPPING
    now = datetime.now(timezone.utc)
    old_transitions = existing.status_transitions or []
    new_transition = {
        "from": previous_status,
        "to": RequestStatus.STOPPING.value,
        "timestamp": now.isoformat(),
    }
    await repo.update_request(
        request_name,
        status=RequestStatus.STOPPING.value,
        status_transitions=old_transitions + [new_transition],
        updated_at=now,
    )
    logger.info("Request %s: %s -> stopping (reason: %s)", request_name, previous_status, body.reason)

    return {
        "request_name": request_name,
        "status": "stopping",
        "previous_status": previous_status,
        "stop_reason": body.reason,
        "message": f"Clean stop initiated for {request_name}",
    }


@router.post("/{request_name}/release", response_model=dict)
async def release_request(
    request_name: str,
    raw_request: FastAPIRequest = None,
    repo: Repository = Depends(get_repository),
):
    """Release a HELD request back to queue, or resume a PAUSED request."""
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    releasable = (RequestStatus.HELD.value, RequestStatus.PAUSED.value)
    if existing.status not in releasable:
        raise HTTPException(
            status_code=400,
            detail=f"Can only release requests in held or paused state, "
                   f"current status: {existing.status}",
        )

    previous_status = existing.status
    # PAUSED needs recovery prep → RESUBMITTING; HELD goes straight to QUEUED
    if previous_status == RequestStatus.PAUSED.value:
        target_status = RequestStatus.RESUBMITTING.value
    else:
        target_status = RequestStatus.QUEUED.value

    now = datetime.now(timezone.utc)
    old_transitions = existing.status_transitions or []
    new_transition = {
        "from": previous_status,
        "to": target_status,
        "timestamp": now.isoformat(),
    }
    await repo.update_request(
        request_name,
        status=target_status,
        status_transitions=old_transitions + [new_transition],
        updated_at=now,
    )
    logger.info("Request %s: %s -> %s (operator release)", request_name, previous_status, target_status)

    return {
        "request_name": request_name,
        "status": target_status,
        "previous_status": previous_status,
        "message": f"Request {request_name} {'resumed' if previous_status == 'paused' else 'released to admission queue'}",
    }


@router.post("/{request_name}/fail", response_model=dict)
async def fail_request(
    request_name: str,
    raw_request: FastAPIRequest = None,
    repo: Repository = Depends(get_repository),
):
    """Fail a HELD or PARTIAL request: kill DAG, mark resources failed.

    Performs all DB work in the API session (auto-committed by get_session)
    rather than delegating to the lifecycle manager's session.
    """
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    allowed = (RequestStatus.HELD.value, RequestStatus.PARTIAL.value, RequestStatus.PAUSED.value)
    if existing.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Can only fail requests in held, partial, or paused state, "
                   f"current status: {existing.status}",
        )

    previous_status = existing.status

    # Kill running DAG via condor_rm
    workflow = await repo.get_workflow_by_request(request_name)
    if workflow:
        if workflow.dag_id:
            dag = await repo.get_dag(workflow.dag_id)
            if dag and dag.status in (DAGStatus.SUBMITTED.value, DAGStatus.RUNNING.value):
                condor = getattr(raw_request.app.state, "condor", None)
                if condor:
                    try:
                        await condor.remove_job(
                            schedd_name=dag.schedd_name,
                            cluster_id=dag.dagman_cluster_id,
                        )
                    except Exception:
                        logger.warning("Failed to remove DAG %s", dag.id)

        # Mark non-terminal DAGs as failed
        for dag in await repo.list_dags(workflow_id=workflow.id):
            if dag.status not in (DAGStatus.FAILED.value, DAGStatus.COMPLETED.value):
                await repo.update_dag(dag.id, status=DAGStatus.FAILED.value)

        # Mark open processing blocks as failed
        for block in await repo.get_processing_blocks(workflow.id):
            if block.status == "open":
                await repo.update_processing_block(block.id, status="failed")

        # Mark workflow as failed
        await repo.update_workflow(workflow.id, status="failed")

    # Transition to FAILED
    now = datetime.now(timezone.utc)
    old_transitions = existing.status_transitions or []
    new_transition = {
        "from": previous_status,
        "to": RequestStatus.FAILED.value,
        "timestamp": now.isoformat(),
    }
    await repo.update_request(
        request_name,
        status=RequestStatus.FAILED.value,
        status_transitions=old_transitions + [new_transition],
        updated_at=now,
    )
    logger.info("Request %s: %s -> failed (operator-initiated)", request_name, previous_status)

    return {
        "request_name": request_name,
        "status": "failed",
        "previous_status": previous_status,
        "message": f"Request {request_name} marked as failed",
    }


@router.delete("/{request_name}", response_model=dict)
async def delete_request(
    request_name: str,
    repo: Repository = Depends(get_repository),
):
    """Delete a failed or aborted request and all related rows."""
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    deletable = ("failed", "aborted")
    if existing.status not in deletable:
        raise HTTPException(
            status_code=400,
            detail=f"Can only delete requests in failed/aborted state, "
                   f"current status: {existing.status}",
        )

    workflow = await repo.get_workflow_by_request(request_name)
    if workflow:
        for dag in await repo.list_dags(workflow_id=workflow.id):
            await repo.delete_dag_history(dag.id)
            await repo.delete_dag(dag.id)
        for block in await repo.get_processing_blocks(workflow.id):
            await repo.delete_processing_block(block.id)
        await repo.delete_workflow(workflow.id)
    await repo.delete_request(request_name)
    logger.info("Deleted request %s (was %s)", request_name, existing.status)

    return {
        "request_name": request_name,
        "message": f"Request {request_name} deleted",
    }


@router.post("/{request_name}/restart", response_model=dict)
async def restart_request(
    request_name: str,
    raw_request: FastAPIRequest = None,
    repo: Repository = Depends(get_repository),
):
    """Kill+clone: create new request with incremented version, fail old."""
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    allowed = (
        RequestStatus.HELD.value, RequestStatus.PARTIAL.value,
        RequestStatus.ABORTED.value, RequestStatus.FAILED.value,
        RequestStatus.PAUSED.value,
    )
    if existing.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Can only restart requests in held, partial, paused, aborted, or failed state, "
                   f"current status: {existing.status}",
        )

    lm = getattr(raw_request.app.state, "lifecycle_manager", None)
    if lm is None:
        raise HTTPException(status_code=503, detail="Lifecycle manager not available")

    new_name = await lm.restart_request(request_name)
    return {
        "request_name": request_name,
        "new_request_name": new_name,
        "status": "failed",
        "message": f"Request {request_name} restarted as {new_name}",
    }


@router.post("/{request_name}/clone", response_model=dict)
async def clone_request(
    request_name: str,
    raw_request: FastAPIRequest = None,
    repo: Repository = Depends(get_repository),
    settings=Depends(get_settings),
):
    """Kill, clear, and re-import with incremented processing_version.

    Equivalent to: stop → fail → delete → import (same parameters, new version).
    The request name stays the same. All old data is cleaned up.
    """
    from wms2.api.import_endpoint import ImportBody, import_request

    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    reqdata = existing.request_data or {}
    workflow = await repo.get_workflow_by_request(request_name)
    config_data = (workflow.config_data if workflow else None) or {}

    # Determine new processing version
    current_version = reqdata.get("ProcessingVersion",
                                  reqdata.get("processing_version", 1))
    new_version = current_version + 1

    # 1. Kill running DAG
    if workflow and workflow.dag_id:
        dag = await repo.get_dag(workflow.dag_id)
        if dag and dag.status in (DAGStatus.SUBMITTED.value, DAGStatus.RUNNING.value):
            condor = getattr(raw_request.app.state, "condor", None)
            if condor:
                try:
                    await condor.remove_job(
                        schedd_name=dag.schedd_name,
                        cluster_id=dag.dagman_cluster_id,
                    )
                    logger.info("Clone %s: removed DAG %s from %s",
                                request_name, dag.dagman_cluster_id, dag.schedd_name)
                except Exception:
                    logger.warning("Clone %s: failed to remove DAG %s",
                                   request_name, dag.dagman_cluster_id, exc_info=True)

    # 2. Delete all old records (DAGs, blocks, workflow, request)
    if workflow:
        for d in await repo.list_dags(workflow_id=workflow.id):
            await repo.delete_dag_history(d.id)
            await repo.delete_dag(d.id)
        for block in await repo.get_processing_blocks(workflow.id):
            await repo.delete_processing_block(block.id)
        await repo.delete_workflow(workflow.id)
    await repo.delete_request(request_name)
    await repo.session.flush()
    logger.info("Clone %s: cleared old data (was %s, version %d)",
                request_name, existing.status, current_version)

    # 3. Re-import with same parameters + incremented version
    #    Reconstruct ImportBody from the old config_data
    priority_profile = config_data.get("priority_profile", {})
    import_body = ImportBody(
        request_name=request_name,
        stageout_mode=config_data.get("stageout_mode", "test"),
        condor_pool=config_data.get("condor_pool", "global"),
        test_fraction=config_data.get("test_fraction"),
        processing_version=new_version,
        allowed_sites=config_data.get("allowed_sites", ""),
        work_units_per_round=config_data.get("work_units_per_round"),
        high_priority=priority_profile.get("high", 5),
        nominal_priority=priority_profile.get("nominal", 3),
        priority_switch_fraction=priority_profile.get("switch_fraction", 0.5),
        replace=True,
    )

    result = await import_request(import_body, raw_request, repo, settings)
    result["cloned_from_version"] = current_version
    result["new_version"] = new_version
    logger.info("Clone %s: re-imported with version %d → %d",
                request_name, current_version, new_version)
    return result


@router.get("/{request_name}/errors", response_model=dict)
async def get_request_errors(
    request_name: str,
    raw_request: FastAPIRequest = None,
    repo: Repository = Depends(get_repository),
):
    """Get error summary for a request (any state)."""
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    lm = getattr(raw_request.app.state, "lifecycle_manager", None)
    if lm is None:
        raise HTTPException(status_code=503, detail="Lifecycle manager not available")

    return await lm.get_error_summary(request_name)


@router.get("/{request_name}/versions", response_model=list[Request])
async def get_request_versions(
    request_name: str,
    repo: Repository = Depends(get_repository),
):
    row = await repo.get_request(request_name)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")

    versions = [Request.model_validate(row)]
    # Walk version chain backward
    current = row
    while current.previous_version_request:
        prev = await repo.get_request(current.previous_version_request)
        if not prev:
            break
        versions.insert(0, Request.model_validate(prev))
        current = prev
    # Walk version chain forward
    current = row
    while current.superseded_by_request:
        nxt = await repo.get_request(current.superseded_by_request)
        if not nxt:
            break
        versions.append(Request.model_validate(nxt))
        current = nxt

    return versions


@router.patch("/{request_name}/priority-profile", response_model=dict)
async def update_priority_profile(
    request_name: str,
    body: PriorityProfileUpdate,
    repo: Repository = Depends(get_repository),
):
    """Update the priority profile for a request (high, nominal, switch_fraction)."""
    existing = await repo.get_request(request_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Request not found")

    if body.switch_fraction < 0 or body.switch_fraction > 1:
        raise HTTPException(status_code=422, detail="switch_fraction must be between 0 and 1")

    profile = {"high": body.high, "nominal": body.nominal, "switch_fraction": body.switch_fraction}

    # Update request_data._priority_profile
    rd = dict(existing.request_data or {})
    # Preserve pilot from existing profile if present
    old_profile = rd.get("_priority_profile", {})
    profile["pilot"] = old_profile.get("pilot", body.high)
    rd["_priority_profile"] = profile
    await repo.update_request(request_name, request_data=rd)

    # Update workflow config_data.priority_profile
    workflow = await repo.get_workflow_by_request(request_name)
    if workflow:
        cd = dict(workflow.config_data or {})
        cd["priority_profile"] = profile
        await repo.update_workflow(workflow.id, config_data=cd)

    logger.info("Priority profile updated for %s: %s", request_name, profile)
    return {"request_name": request_name, "priority_profile": profile}
