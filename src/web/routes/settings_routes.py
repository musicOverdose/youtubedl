from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.database import get_db
from src.services.audit_service import AuditService
from src.services.ytdlp_service import YtDlpService
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
async def get_settings(admin: dict = Depends(get_current_admin)):
    return {
        "max_video_duration_seconds": settings.MAX_VIDEO_DURATION_SECONDS,
        "max_video_duration_formatted": YtDlpService.format_duration(settings.MAX_VIDEO_DURATION_SECONDS),
        "allow_unknown_duration": settings.ALLOW_UNKNOWN_DURATION,
        "cache_hit_bypasses_duration_limit": settings.CACHE_HIT_BYPASSES_DURATION_LIMIT,
        "max_concurrent_per_user": settings.MAX_CONCURRENT_PER_USER,
        "max_queued_per_user": settings.MAX_QUEUED_PER_USER,
        "max_temp_storage_gb": settings.MAX_TEMP_STORAGE_GB,
        "default_max_height": settings.DEFAULT_MAX_HEIGHT,
        "playlists_enabled": settings.PLAYLISTS_ENABLED,
        "h264_enabled": settings.H264_ENABLED,
        "h265_enabled": settings.H265_ENABLED,
        "mp3_enabled": settings.MP3_ENABLED,
        "subtitles_enabled": settings.SUBTITLES_ENABLED,
    }


@router.post("")
async def update_settings(
    req: SettingsUpdateRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    settings.MAX_VIDEO_DURATION_SECONDS = req.max_video_duration_seconds
    settings.ALLOW_UNKNOWN_DURATION = req.allow_unknown_duration
    settings.CACHE_HIT_BYPASSES_DURATION_LIMIT = req.cache_hit_bypasses_duration_limit
    settings.MAX_CONCURRENT_PER_USER = req.max_concurrent_per_user
    settings.MAX_QUEUED_PER_USER = req.max_queued_per_user
    settings.MAX_TEMP_STORAGE_GB = req.max_temp_storage_gb
    settings.DEFAULT_MAX_HEIGHT = req.default_max_height
    settings.PLAYLISTS_ENABLED = req.playlists_enabled
    settings.H264_ENABLED = req.h264_enabled
    settings.H265_ENABLED = req.h265_enabled
    settings.MP3_ENABLED = req.mp3_enabled
    settings.SUBTITLES_ENABLED = req.subtitles_enabled

    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], "Updated global runtime application settings"
    )
    return {"status": "updated"}
