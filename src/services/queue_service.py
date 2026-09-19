import json
from typing import Dict, List, Optional, Tuple
import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import (
    REDIS_KEY_ACTIVE_JOBS,
    REDIS_KEY_CACHE_LOCK_PREFIX,
    REDIS_KEY_CANCEL_PREFIX,
    REDIS_KEY_PROGRESS_PREFIX,
    REDIS_KEY_QUEUE,
    REDIS_KEY_QUEUE_PAUSED,
    JobStatus,
)
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.job import Job
from src.models.job_request import JobRequest

logger = setup_logger("queue_service")

# Atomic slot reservation Lua script
LUA_ACQUIRE_JOB = """
local is_paused = redis.call('GET', KEYS[3])
if is_paused == '1' then
    return nil
end

local active_count = redis.call('SCARD', KEYS[2])
local max_active = tonumber(ARGV[1])

if active_count >= max_active then
    return nil
end

local job_id = redis.call('LPOP', KEYS[1])
if job_id then
    redis.call('SADD', KEYS[2], job_id)
    return job_id
end

return nil
"""


class QueueService:
    @staticmethod
    async def is_paused() -> bool:
        r = get_redis_client()
        val = await r.get(REDIS_KEY_QUEUE_PAUSED)
        return val == "1"

    @staticmethod
    async def pause_queue() -> None:
        r = get_redis_client()
        await r.set(REDIS_KEY_QUEUE_PAUSED, "1")
        logger.info("Global queue has been PAUSED by admin")

    @staticmethod
    async def resume_queue() -> None:
        r = get_redis_client()
        await r.delete(REDIS_KEY_QUEUE_PAUSED)
        logger.info("Global queue has been RESUMED by admin")

    @staticmethod
    async def push_job(job_id: str) -> int:
        """
        Pushes a new job ID to the end of the FIFO queue (RPUSH).
        Returns the derived 1-based queue position.
        """
        r = get_redis_client()
        await r.rpush(REDIS_KEY_QUEUE, job_id)
        queue_len = await r.llen(REDIS_KEY_QUEUE)
        logger.info(f"Job {job_id} pushed to FIFO queue. Current queue length: {queue_len}")
        return queue_len

    @staticmethod
    async def acquire_next_job(max_active_jobs: int) -> Optional[str]:
        """
        Atomically checks queue pause state and active jobs limit.
        If slot is available, pops the next job from the queue and marks it active in Redis.
        """
        r = get_redis_client()
        job_id = await r.eval(
            LUA_ACQUIRE_JOB,
            3,
            REDIS_KEY_QUEUE,
            REDIS_KEY_ACTIVE_JOBS,
            REDIS_KEY_QUEUE_PAUSED,
            max_active_jobs,
        )
        return job_id

    @staticmethod
    async def release_active_job(job_id: str) -> None:
        """Removes a job from the active set upon completion, failure, or cancellation."""
        r = get_redis_client()
        await r.srem(REDIS_KEY_ACTIVE_JOBS, job_id)
        logger.info(f"Job {job_id} removed from active set")

    @staticmethod
    async def get_active_job_ids() -> List[str]:
        r = get_redis_client()
        members = await r.smembers(REDIS_KEY_ACTIVE_JOBS)
        return list(members)

    @staticmethod
    async def get_queued_job_ids() -> List[str]:
        r = get_redis_client()
        return await r.lrange(REDIS_KEY_QUEUE, 0, -1)

    @staticmethod
    async def get_derived_position(job_id: str) -> Optional[int]:
        """
        Calculates derived 1-based queue position.
        Position = number of valid queued jobs ahead + 1.
        Returns None if job is not currently queued.
        """
        r = get_redis_client()
        queued = await r.lrange(REDIS_KEY_QUEUE, 0, -1)
        try:
            idx = queued.index(job_id)
            return idx + 1
        except ValueError:
            return None

    @staticmethod
    async def remove_from_queue(job_id: str) -> bool:
        """Removes a job from the FIFO queue (e.g. on cancellation before starting)."""
        r = get_redis_client()
        removed = await r.lrem(REDIS_KEY_QUEUE, 0, job_id)
        return removed > 0

    @staticmethod
    async def acquire_cache_lock(cache_key: str, job_id: str, ttl_seconds: int = 3600) -> bool:
        """
        Prevents multiple workers or requests from creating duplicate jobs for the same cache key.
        """
        r = get_redis_client()
        lock_key = f"{REDIS_KEY_CACHE_LOCK_PREFIX}{cache_key}"
        # Set if not exists
        acquired = await r.set(lock_key, job_id, nx=True, ex=ttl_seconds)
        return bool(acquired)

    @staticmethod
    async def get_cache_lock_owner(cache_key: str) -> Optional[str]:
        r = get_redis_client()
        lock_key = f"{REDIS_KEY_CACHE_LOCK_PREFIX}{cache_key}"
        return await r.get(lock_key)

    @staticmethod
    async def release_cache_lock(cache_key: str) -> None:
        r = get_redis_client()
        lock_key = f"{REDIS_KEY_CACHE_LOCK_PREFIX}{cache_key}"
        await r.delete(lock_key)

    @staticmethod
    async def set_progress(
        job_id: str,
        stage: str,
        progress_pct: float = 0.0,
        speed: str = "",
        eta: str = "",
    ) -> None:
        r = get_redis_client()
        data = {
            "stage": stage,
            "progress": round(progress_pct, 1),
            "speed": speed,
            "eta": eta,
        }
        await r.set(f"{REDIS_KEY_PROGRESS_PREFIX}{job_id}", json.dumps(data), ex=3600)

    @staticmethod
    async def get_progress(job_id: str) -> Dict:
        r = get_redis_client()
        val = await r.get(f"{REDIS_KEY_PROGRESS_PREFIX}{job_id}")
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return {"stage": "Preparing", "progress": 0.0, "speed": "", "eta": ""}

    @staticmethod
    async def signal_cancel(job_id: str) -> None:
        r = get_redis_client()
        await r.set(f"{REDIS_KEY_CANCEL_PREFIX}{job_id}", "1", ex=3600)

    @staticmethod
    async def is_cancelled(job_id: str) -> bool:
        r = get_redis_client()
        val = await r.get(f"{REDIS_KEY_CANCEL_PREFIX}{job_id}")
        return val == "1"

    @classmethod
    async def check_user_limits(
        cls, session: AsyncSession, user_id: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Enforces MAX_CONCURRENT_PER_USER and MAX_QUEUED_PER_USER.
        """
        # Count active/queued jobs for this user
        stmt = (
            select(func.count(JobRequest.id))
            .join(Job, Job.id == JobRequest.job_id)
            .where(
                JobRequest.user_id == user_id,
                Job.status.in_([
                    JobStatus.QUEUED.value,
                    JobStatus.PREPARING.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.PROCESSING.value,
                    JobStatus.UPLOADING.value,
                ]),
            )
        )
        res = await session.execute(stmt)
        active_or_queued_count = res.scalar() or 0

        if active_or_queued_count >= settings.MAX_CONCURRENT_PER_USER + settings.MAX_QUEUED_PER_USER:
            return False, f"⚠️ You have reached the maximum allowed concurrent/queued jobs limit ({active_or_queued_count}). Please wait for your current tasks to finish."

        return True, None
