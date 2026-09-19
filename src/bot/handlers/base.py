from datetime import datetime, timezone
from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.user import User

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
        await get_or_create_user(session, message.from_user)

    welcome_text = (
        f"👋 Hello, <b>{message.from_user.first_name}</b>!\n\n"
        "Send me any YouTube video or Shorts link, and I will download it for you in high quality.\n\n"
        "✨ <b>Features:</b>\n"
        "• Exact Video Resolutions (up to 4K)\n"
        "• 🎬 H.264 & 📦 H.265 / AAC options\n"
        "• 🎵 High-quality MP3 with ID3 cover art\n"
        "• 💬 Subtitles in 🇬🇧 English & 🇮🇷 Persian\n"
        "• Instant delivery for cached media"
    )
    await message.answer(welcome_text)


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
