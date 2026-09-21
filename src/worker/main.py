import asyncio
from pathlib import Path
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

    # Load persistent application settings from PostgreSQL
    from src.services.setting_service import SettingService
    try:
        await SettingService.load_all_settings_to_runtime()
    except Exception as e:
        logger.warning("Could not load application settings from DB in worker: %s", e)

    # Listen for runtime config reloads via Redis
    async def listen_for_setting_reloads():
        from src.core.redis import get_redis_client
        while running:
            try:
                r = get_redis_client()
                pubsub = r.pubsub()
                await pubsub.subscribe("app:config:reload")
                async for message in pubsub.listen():
                    if not running:
                        break
                    if message and message.get("type") == "message":
                        try:
                            await SettingService.load_all_settings_to_runtime()
                            logger.info("Worker reloaded application settings from PostgreSQL.")
                        except Exception as e:
                            logger.warning("Worker failed to reload application settings: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Worker settings reload pubsub error: %s", e)
                await asyncio.sleep(3.0)

    setting_reload_task = asyncio.create_task(listen_for_setting_reloads())

    # Launch telemetry task
    telemetry_task = asyncio.create_task(periodic_telemetry(processor))

    # 2. Main consumer loop
    logger.info(f"Worker listening for jobs (Concurrency limit: {settings.MAX_ACTIVE_JOBS})...")

    try:
        while running:
            try:
                # Gating: if READY flag is absent, pause job acquisition
                ready_file = Path(settings.RUNTIME_READY_FILE)
                if not ready_file.is_file():
                    logger.debug("System not READY for Telegram processing (%s missing). Pausing job acquisition...", ready_file)
                    await asyncio.sleep(1.0)
                    continue

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
        setting_reload_task.cancel()

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
