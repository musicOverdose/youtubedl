import asyncio
import signal
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.queue_service import QueueService
from src.worker.processor import JobProcessor
from src.worker.recovery import WorkerRecovery

logger = setup_logger("worker_main")
running = True


def handle_stop_signals():
    global running
    logger.info("Shutdown signal received. Stopping worker loop...")
    running = False


async def periodic_telemetry(processor: JobProcessor):
    while running:
        try:
            await processor.report_telemetry()
        except Exception as e:
            logger.debug("Error in telemetry loop: %s", e)
        await asyncio.sleep(15)


async def worker_loop():
    global running
    logger.info("Starting background worker...")

    processor = JobProcessor()

    # 1. Startup recovery
    async with AsyncSessionLocal() as session:
        await WorkerRecovery.perform_startup_recovery(session)

    # Launch telemetry task
    telemetry_task = asyncio.create_task(periodic_telemetry(processor))

    # 2. Main consumer loop
    logger.info(f"Worker listening for jobs (Concurrency limit: {settings.MAX_ACTIVE_JOBS})...")

    try:
        while running:
            try:
                # Atomically attempt to acquire the next job slot
                job_id = await QueueService.acquire_next_job(settings.MAX_ACTIVE_JOBS)

                if job_id:
                    logger.info(f"Acquired job {job_id}. Starting execution...")
                    # Run processing task
                    asyncio.create_task(processor.process_job(job_id))
                else:
                    await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in worker consumer loop: {e}", exc_info=True)
                await asyncio.sleep(2.0)
    finally:
        telemetry_task.cancel()

    logger.info("Worker consumer loop finished.")


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_stop_signals)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(worker_loop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
