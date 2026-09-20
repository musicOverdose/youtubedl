import asyncio
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from src.bot.handlers.audio_handler import audio_router
from src.bot.handlers.base import base_router
from src.bot.handlers.codec_handler import codec_router
from src.bot.handlers.must_join_handler import must_join_router
from src.bot.handlers.quality_handler import quality_router
from src.bot.handlers.queue_handler import queue_router
from src.bot.handlers.subtitle_handler import subtitle_router
from src.bot.handlers.url_handler import url_router
from src.core.config import settings
from src.core.logger import setup_logger
from src.core.redis import get_redis_client

logger = setup_logger("bot_main")


def setup_handlers(dp: Dispatcher) -> None:
    dp.include_router(base_router)
    dp.include_router(url_router)
    dp.include_router(quality_router)
    dp.include_router(codec_router)
    dp.include_router(audio_router)
    dp.include_router(subtitle_router)
    dp.include_router(queue_router)
    dp.include_router(must_join_router)


def read_runtime_token() -> Optional[str]:
    token_path = Path(settings.RUNTIME_BOT_TOKEN_FILE)
    if token_path.is_file():
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception as e:
            logger.warning("Failed to read %s: %s", token_path, e)
    return settings.BOT_TOKEN if settings.BOT_TOKEN else None


async def read_runtime_mode() -> str:
    try:
        r = get_redis_client()
        m = await r.get("telegram:active:mode")
        if m:
            return m.decode("utf-8") if isinstance(m, bytes) else str(m)
    except Exception as e:
        logger.debug("Failed reading mode from Redis: %s", e)
    return settings.TELEGRAM_API_MODE or "local"


async def wait_for_readiness() -> None:
    ready_file = Path(settings.RUNTIME_READY_FILE)
    while not ready_file.is_file():
        logger.info("Waiting for runtime state reconciliation (%s)...", ready_file)
        await asyncio.sleep(2)
    logger.info("Startup readiness confirmed (%s exists).", ready_file)


async def main() -> None:
    logger.info("Initializing Telegram Bot Service...")

    # 1. Startup readiness gating
    await wait_for_readiness()

    dp = Dispatcher()
    setup_handlers(dp)

    reload_event = asyncio.Event()

    async def listen_for_reloads():
        try:
            r = get_redis_client()
            pubsub = r.pubsub()
            await pubsub.subscribe("telegram:config:reload")
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    logger.info("Configuration reload event received via Redis. Cycling Bot session...")
                    reload_event.set()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Redis pubsub listener encountered error: %s", e)

    reload_listener_task = asyncio.create_task(listen_for_reloads())

    try:
        while True:
            reload_event.clear()
            token = read_runtime_token()
            if not token:
                logger.info("Bot token not configured. Waiting for configuration in Web Admin...")
                # Wait until token file appears or reload event fires
                while not token and not reload_event.is_set():
                    await asyncio.sleep(2)
                    token = read_runtime_token()
                if not token:
                    continue

            mode = await read_runtime_mode()
            logger.info("Configuring Bot with mode: %s", mode)

            if mode == "local":
                session = AiohttpSession(
                    api=TelegramAPIServer.from_base(settings.TELEGRAM_API_BASE_URL, is_local=True)
                )
            else:
                session = AiohttpSession(
                    api=TelegramAPIServer.from_base("https://api.telegram.org")
                )

            bot = Bot(
                token=token,
                session=session,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )

            polling_task = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=["message", "callback_query"])
            )

            # Wait until reload event is signaled or polling task ends
            waiter = asyncio.create_task(reload_event.wait())
            done, pending = await asyncio.wait(
                [polling_task, waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if polling_task in done:
                # Polling terminated on its own
                exc = polling_task.exception() if not polling_task.cancelled() else None
                if exc:
                    logger.error("Bot polling stopped with error: %s", exc)
                waiter.cancel()
            else:
                # Reload triggered: cancel polling and restart
                logger.info("Stopping current polling instance for config reload...")
                polling_task.cancel()
                try:
                    await polling_task
                except (asyncio.CancelledError, Exception):
                    pass

            await bot.session.close()
            await asyncio.sleep(1)

    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Bot service shutdown requested.")
    finally:
        reload_listener_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
