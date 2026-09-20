from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import JobStatus
from src.core.database import get_db
from src.core.redis import get_redis_client
from src.models.cache import CacheEntry
from src.models.job import Job
from src.services.queue_service import QueueService
from src.services.system_service import SystemService
from src.web.auth import get_current_admin

router = APIRouter(tags=["System"])


@router.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


@router.get("/ready")
async def readiness_check(session: AsyncSession = Depends(get_db)):
    db_ok = False
    redis_ok = False

    # Check DB
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    # Check Redis
    try:
        r = get_redis_client()
        await r.ping()
        redis_ok = True
    except Exception:
        pass

    all_ready = db_ok and redis_ok
    status_code = 200 if all_ready else 503
    return Response(
        content=f'{{"status": "{"ready" if all_ready else "not_ready"}", "database": {str(db_ok).lower()}, "redis": {str(redis_ok).lower()}}}',
        media_type="application/json",
        status_code=status_code,
    )


@router.get("/metrics")
async def get_prometheus_metrics(session: AsyncSession = Depends(get_db)):
    completed = await session.scalar(
        select(func.count(Job.id)).where(Job.status == JobStatus.COMPLETED.value)
    ) or 0
    failed = await session.scalar(
        select(func.count(Job.id)).where(Job.status == JobStatus.FAILED.value)
    ) or 0
    cache_hits = await session.scalar(
        select(func.sum(CacheEntry.hit_count)).where(CacheEntry.is_valid == True)
    ) or 0
    cache_entries = await session.scalar(
        select(func.count(CacheEntry.id)).where(CacheEntry.is_valid == True)
    ) or 0

    active_jobs = len(await QueueService.get_active_job_ids())
    queued_jobs = len(await QueueService.get_queued_job_ids())

    metrics_text = (
        f"# HELP ytdl_jobs_completed_total Total completed jobs\n"
        f"# TYPE ytdl_jobs_completed_total counter\n"
        f"ytdl_jobs_completed_total {completed}\n\n"
        f"# HELP ytdl_jobs_failed_total Total failed jobs\n"
        f"# TYPE ytdl_jobs_failed_total counter\n"
        f"ytdl_jobs_failed_total {failed}\n\n"
        f"# HELP ytdl_cache_hits_total Total media cache hits\n"
        f"# TYPE ytdl_cache_hits_total counter\n"
        f"ytdl_cache_hits_total {cache_hits}\n\n"
        f"# HELP ytdl_cache_entries_total Valid cache entries stored\n"
        f"# TYPE ytdl_cache_entries_total gauge\n"
        f"ytdl_cache_entries_total {cache_entries}\n\n"
        f"# HELP ytdl_queue_active_jobs Number of currently active processing jobs\n"
        f"# TYPE ytdl_queue_active_jobs gauge\n"
        f"ytdl_queue_active_jobs {active_jobs}\n\n"
        f"# HELP ytdl_queue_waiting_jobs Number of jobs waiting in FIFO queue\n"
        f"# TYPE ytdl_queue_waiting_jobs gauge\n"
        f"ytdl_queue_waiting_jobs {queued_jobs}\n"
    )

    return Response(content=metrics_text, media_type="text/plain")


@router.get("/api/system")
async def get_system_info(admin: dict = Depends(get_current_admin)):
    stats = await SystemService.get_system_stats()
    tools = await SystemService.get_tool_versions()
    return {
        "stats": stats,
        "tools": tools,
    }
