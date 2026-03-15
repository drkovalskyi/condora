from fastapi import APIRouter, Depends

from condora.config import Settings
from condora.core.admission_controller import AdmissionController
from condora.db.repository import Repository

from .deps import get_repository, get_settings

router = APIRouter(prefix="/admission", tags=["admission"])


@router.get("/queue")
async def get_queue_status(
    repo: Repository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
):
    controller = AdmissionController(repo, settings)
    return await controller.get_queue_status()
