from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import JobStatus
from src.core.database import get_db
from src.models.cache import CacheEntry
from src.models.channel import RequiredChannel
from src.models.job import Job
from src.services.ai_service import AIService
from src.services.cookie_service import CookieService
from src.services.queue_service import QueueService
from src.services.system_service import SystemService
from src.services.ytdlp_service import YtDlpService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


@router.get("/stats")
async def get_dashboard_stats(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    system_stats = await SystemService.get_system_stats()
    tool_versions = await SystemService.get_tool_versions()

    # Database stats
    completed_count = await session.scalar(
        select(func.count(Job.id)).where(Job.status == JobStatus.COMPLETED.value)
    ) or 0
    failed_count = await session.scalar(
        select(func.count(Job.id)).where(Job.status == JobStatus.FAILED.value)
    ) or 0

    cache_count = await session.scalar(
        select(func.count(CacheEntry.id)).where(CacheEntry.is_valid == True)
    ) or 0

    total_hits = await session.scalar(
        select(func.sum(CacheEntry.hit_count)).where(CacheEntry.is_valid == True)
    ) or 0

    total_requests = completed_count + total_hits
    hit_rate = round((total_hits / total_requests * 100), 1) if total_requests > 0 else 0.0

    # Required channels count
    req_channels_count = await session.scalar(
        select(func.count(RequiredChannel.id)).where(RequiredChannel.enabled == True)
    ) or 0

    # Queue stats
    active_jobs = await QueueService.get_active_job_ids()
    queued_jobs = await QueueService.get_queued_job_ids()
    is_paused = await QueueService.is_paused()

    cookie_status = CookieService.get_status()

    return {
        "system": system_stats,
        "tools": tool_versions,
        "queue": {
            "active_count": len(active_jobs),
            "max_active": settings.MAX_ACTIVE_JOBS,
            "queued_count": len(queued_jobs),
            "is_paused": is_paused,
        },
        "cache": {
            "entries_count": cache_count,
            "total_hits": total_hits,
            "hit_rate_pct": hit_rate,
        },
        "jobs": {
            "completed": completed_count,
            "failed": failed_count,
        },
        "settings": {
            "max_video_file_size_mb_local": getattr(settings, "MAX_VIDEO_FILE_SIZE_MB_LOCAL", 1900),
            "max_video_file_size_mb_cloud": getattr(settings, "MAX_VIDEO_FILE_SIZE_MB_CLOUD", 48),
            "max_video_size_formatted": f"{getattr(settings, 'MAX_VIDEO_FILE_SIZE_MB_LOCAL', 1900)} MB",
            "max_duration_seconds": getattr(settings, "MAX_VIDEO_DURATION_SECONDS", 7200),
            "max_duration_formatted": YtDlpService.format_duration(getattr(settings, "MAX_VIDEO_DURATION_SECONDS", 7200)),
            "must_join_enabled": settings.MUST_JOIN_ENABLED,
            "required_channels_count": req_channels_count,
            "ai_enabled": settings.AI_ENABLED,
            "ai_configured": AIService.is_configured(),
            "cookies_enabled": settings.YTDLP_COOKIES_ENABLED,
            "cookies_configured": cookie_status["configured"],
        },
    }
