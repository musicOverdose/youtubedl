from typing import Optional
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from src.core.config import settings

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        if (
            settings.TELEGRAM_API_BASE_URL
            and settings.TELEGRAM_API_BASE_URL.rstrip("/") != "https://api.telegram.org"
        ):
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(settings.TELEGRAM_API_BASE_URL)
            )
            _bot = Bot(
                token=settings.BOT_TOKEN,
                session=session,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        else:
            _bot = Bot(
                token=settings.BOT_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
    return _bot


def get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        _dp = Dispatcher()
    return _dp
