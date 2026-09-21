from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import get_db
from src.services.audit_service import AuditService
from src.services.setting_service import SettingService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/settings", tags=["Settings"])


class SettingsUpdateRequest(BaseModel):
    max_video_duration_seconds: int
    allow_unknown_duration: bool
    cache_hit_bypasses_duration_limit: bool
    max_concurrent_per_user: int
    max_queued_per_user: int
    max_temp_storage_gb: int
    default_max_height: int
    playlists_enabled: bool
    h264_enabled: bool
    h265_enabled: bool
    mp3_enabled: bool
    subtitles_enabled: bool


@router.get("")
async def get_settings(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    return await SettingService.get_application_settings(session)


@router.post("")
async def update_settings(
    req: SettingsUpdateRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    await SettingService.save_application_settings(req.model_dump(), session)
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], "Updated global runtime application settings"
    )
    return {"status": "updated"}

