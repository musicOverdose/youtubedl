from pathlib import Path
from typing import Tuple
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from src.core.config import settings
from src.core.logger import get_logger
from src.core.redis import get_redis_client

logger = get_logger("telegram_factory")


class TelegramClientFactory:
    @staticmethod
    async def get_client() -> Tuple[Bot, str]:
        """
        Creates a Bot instance dynamically according to runtime configuration.
        Worker MUST NEVER decrypt credentials directly from PostgreSQL.
        Reads token exclusively from /config/runtime/bot-token (or settings.BOT_TOKEN fallback).
        Reads mode exclusively from Redis cache (or settings.TELEGRAM_API_MODE fallback).
        """
        token_path = Path(settings.RUNTIME_BOT_TOKEN_FILE)
        if token_path.is_file():
            token = token_path.read_text(encoding="utf-8").strip()
        else:
            token = settings.BOT_TOKEN

        if not token:
            raise RuntimeError(
                f"Bot token not available at {settings.RUNTIME_BOT_TOKEN_FILE} or in environment."
            )

        mode = "local"
        try:
            r = get_redis_client()
            cached_mode = await r.get("telegram:active:mode")
            if cached_mode:
                mode = (
                    cached_mode.decode("utf-8")
                    if isinstance(cached_mode, bytes)
                    else str(cached_mode)
                )
            else:
                mode = settings.TELEGRAM_API_MODE or "local"
        except Exception as e:
            logger.debug("Could not read telegram mode from Redis: %s", e)
            mode = settings.TELEGRAM_API_MODE or "local"

        if mode == "local":
            base_url = settings.TELEGRAM_API_BASE_URL
            if not base_url or "api.telegram.org" in base_url:
                base_url = "http://telegram-bot-api:8081"
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(base_url, is_local=True)
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
        return bot, mode
