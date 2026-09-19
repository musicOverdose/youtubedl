import asyncio
import signal
from src.bot.bot_instance import get_bot, get_dispatcher
from src.bot.handlers.audio_handler import audio_router
from src.bot.handlers.base import base_router
from src.bot.handlers.codec_handler import codec_router
from src.bot.handlers.must_join_handler import must_join_router
from src.bot.handlers.quality_handler import quality_router
from src.bot.handlers.queue_handler import queue_router
from src.bot.handlers.subtitle_handler import subtitle_router
from src.bot.handlers.url_handler import url_router
from src.core.logger import setup_logger

logger = setup_logger("bot_main")


def setup_handlers(dp):
    dp.include_router(base_router)
    dp.include_router(url_router)
    dp.include_router(quality_router)
    dp.include_router(codec_router)
    dp.include_router(audio_router)
    dp.include_router(subtitle_router)
    dp.include_router(queue_router)
    dp.include_router(must_join_router)


async def main():
    logger.info("Initializing Telegram Bot...")
    bot = get_bot()
    dp = get_dispatcher()
    setup_handlers(dp)

    logger.info("Starting bot long polling...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Bot polling stopped")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
