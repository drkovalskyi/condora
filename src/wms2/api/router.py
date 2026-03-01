from fastapi import APIRouter

from . import admission, dags, import_endpoint, lifecycle, monitoring, requests, sites, workflows

api_router = APIRouter()
api_router.include_router(requests.router)
api_router.include_router(workflows.router)
api_router.include_router(dags.router)
api_router.include_router(sites.router)
api_router.include_router(admission.router)
api_router.include_router(monitoring.router)
api_router.include_router(lifecycle.router)
api_router.include_router(import_endpoint.router)
