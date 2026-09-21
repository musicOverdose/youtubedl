from aiogram import Bot, Router
from aiogram.types import Message
from src.bot.handlers.base import get_or_create_user
from src.bot.keyboards import build_quality_keyboard
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.must_join_service import MustJoinService
from src.services.ytdlp_service import YtDlpService, extract_youtube_id, get_canonical_url

logger = setup_logger("url_handler")
url_router = Router()


async def process_youtube_url(bot: Bot, chat_id: int, canonical_url: str, source_id: str) -> None:
    """
    Extracts video metadata, validates duration, and sends the quality selection menu.
    Can be called directly or resumed after Must-Join verification.
    """
    status_msg = await bot.send_message(chat_id=chat_id, text="🔎 <i>Extracting video information...</i>")
    try:
        info = await YtDlpService.extract_metadata(canonical_url)
    except Exception as e:
        logger.error(f"Failed to extract metadata for {canonical_url}: {e}")
        try:
            await status_msg.edit_text("❌ Failed to retrieve video details. Please ensure the link is valid and public.")
        except Exception:
            pass
        return

    # Duration validation
    is_valid, duration_secs, err_msg = YtDlpService.validate_duration(
        info, settings.MAX_VIDEO_DURATION_SECONDS, settings.ALLOW_UNKNOWN_DURATION
    )
    if not is_valid:
        try:
            await status_msg.edit_text(err_msg or "❌ This video exceeds the maximum duration limit.")
        except Exception:
            pass
        return

    # Usable resolutions
    available_heights = YtDlpService.get_available_resolutions(info)
    title = info.get("title", "YouTube Video")
    thumbnail_url = info.get("thumbnail")

    try:
        await status_msg.delete()
    except Exception:
        pass

    keyboard = build_quality_keyboard(source_id, available_heights)

    if thumbnail_url:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=thumbnail_url,
                caption=f"<b>{title}</b>",
                reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.warning(f"Could not send thumbnail photo: {e}. Falling back to text.")

    await bot.send_message(
        chat_id=chat_id,
        text=f"🎬 <b>{title}</b>",
        reply_markup=keyboard,
    )


@url_router.message(lambda msg: msg.text and extract_youtube_id(msg.text) is not None)
async def handle_youtube_url(message: Message, bot: Bot):
    user_id = message.from_user.id
    raw_text = message.text.strip()
    source_id = extract_youtube_id(raw_text)
    if not source_id:
        return

    canonical_url = get_canonical_url(source_id)

    async with AsyncSessionLocal() as session:
        await get_or_create_user(session, message.from_user)

        # 1. CENTRALIZED MUST-JOIN CHECK (AUTHORITATIVE)
        allowed = await MustJoinService.enforce_must_join_message(
            message=message,
            bot=bot,
            session=session,
            pending_action={
                "type": "url",
                "payload": {"url": canonical_url, "source_id": source_id},
            },
        )
        if not allowed:
            return

    # 2. PROCEED WITH URL PROCESSING
    await process_youtube_url(bot, message.chat.id, canonical_url, source_id)
