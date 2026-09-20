import html
from datetime import datetime, timezone
from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.user import User
from src.services.setting_service import DEFAULT_WELCOME_MESSAGE, SettingService

logger = setup_logger("bot_base")
base_router = Router()


async def get_or_create_user(session: AsyncSession, msg_user) -> User:
    stmt = select(User).where(User.id == msg_user.id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        user = User(
            id=msg_user.id,
            username=msg_user.username,
            first_name=msg_user.first_name,
            first_seen_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        session.add(user)
    else:
        user.username = msg_user.username
        user.first_name = msg_user.first_name
        user.last_seen_at = datetime.now(timezone.utc)

    await session.commit()
    return user


@base_router.message(CommandStart())
async def cmd_start(message: Message):
    async with AsyncSessionLocal() as session:
        if message.from_user:
            await get_or_create_user(session, message.from_user)
        template = await SettingService.get_welcome_message(session)

    raw_name = message.from_user.first_name if (message.from_user and message.from_user.first_name) else "User"
    safe_name = html.escape(raw_name)
    welcome_text = template.replace("{first_name}", safe_name)

    try:
        await message.answer(welcome_text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "can't parse entities" in err_msg or "entity" in err_msg:
            logger.warning("Failed to render custom welcome message due to HTML entity error: %s. Falling back to default.", e)
            fallback_text = DEFAULT_WELCOME_MESSAGE.replace("{first_name}", safe_name)
            await message.answer(fallback_text, parse_mode=ParseMode.HTML)
        else:
            raise


@base_router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>How to use:</b>\n\n"
        "1. Simply paste a YouTube link in the chat.\n"
        "2. Choose your preferred video resolution, MP3, or Subtitle.\n"
        "3. If you picked video, select H.264 or H.265.\n"
        "4. Your file will be processed and sent right away!"
    )
    await message.answer(help_text)
