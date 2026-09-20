import os
import shutil
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import (
    REDIS_KEY_ACTIVE_JOBS,
    REDIS_KEY_CACHE_LOCK_PREFIX,
    JobStatus,
)
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.job import Job

logger = setup_logger("worker_recovery")


class WorkerRecovery:
    @classmethod
    async def perform_startup_recovery(cls, session: AsyncSession) -> None:
        """
        Executed when the worker boots up:
        - Detects abandoned jobs left in non-terminal states
        - Cleans stale local temporary directories
        - Clears dangling Redis locks
        """
        logger.info("Performing worker startup recovery...")

        # 1. Clean Redis active jobs set from prior worker run
        r = get_redis_client()
        active_job_ids = await r.smembers(REDIS_KEY_ACTIVE_JOBS)
        for jid in active_job_ids:
            await r.srem(REDIS_KEY_ACTIVE_JOBS, jid)
            logger.info(f"Released dangling active lock for job: {jid}")

        # 2. Identify in-flight jobs in database
        in_flight_statuses = [
            JobStatus.PREPARING.value,
            JobStatus.DOWNLOADING.value,
            JobStatus.PROCESSING.value,
            JobStatus.UPLOADING.value,
        ]
        stmt = select(Job).where(Job.status.in_(in_flight_statuses))
        res = await session.execute(stmt)
        abandoned_jobs = res.scalars().all()

        for job in abandoned_jobs:
            logger.warning(f"Recovering abandoned job {job.id} (was {job.status}). Marking FAILED.")
            job.status = JobStatus.FAILED.value
            job.error_code = "WORKER_RESTARTED"
            job.error_message = "Worker process restarted before job could complete."
            # Remove any cache lock for this job
            lock_key = f"{REDIS_KEY_CACHE_LOCK_PREFIX}{job.cache_key}"
            await r.delete(lock_key)

        await session.commit()

        # 3. Clean temporary storage directory /tmp/ytdl/
        temp_dir = settings.TEMP_DIR
        if os.path.exists(temp_dir):
            try:
                for item in os.listdir(temp_dir):
                    item_path = os.path.join(temp_dir, item)
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path, ignore_errors=True)
                        logger.info(f"Cleaned stale temp folder: {item_path}")
            except Exception as e:
                logger.error(f"Error during temp directory cleanup: {e}")

        # 4. Clean transfer staging directory /transfer/
        transfer_dir = settings.TRANSFER_DIR
        if os.path.exists(transfer_dir):
            try:
                for item in os.listdir(transfer_dir):
                    item_path = os.path.join(transfer_dir, item)
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path, ignore_errors=True)
                        logger.info(f"Cleaned stale transfer folder: {item_path}")
            except Exception as e:
                logger.error(f"Error during transfer directory cleanup: {e}")

        logger.info("Startup recovery complete.")
