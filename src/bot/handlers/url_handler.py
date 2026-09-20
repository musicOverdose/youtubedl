from aiogram import Bot, Router
from aiogram.types import Message
from src.bot.handlers.base import get_or_create_user
from src.bot.keyboards import build_must_join_keyboard, build_quality_keyboard
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.must_join_service import MustJoinService
from src.services.ytdlp_service import YtDlpService, extract_youtube_id, get_canonical_url

logger = setup_logger("url_handler")
url_router = Router()


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
        # Checked BEFORE expensive yt-dlp metadata extraction
        is_auth, missing_channels = await MustJoinService.require_must_join(
            bot, session, user_id, force_authoritative=True
        )
        if not is_auth:
            logger.info(f"User {user_id} blocked by Must-Join requirement")
            kb = build_must_join_keyboard(missing_channels, resume_action=canonical_url)
            prompt_text = await MustJoinService.get_rendered_must_join_message(
                session, message.from_user.first_name, missing_channels
            )
            await message.answer(
                prompt_text,
                reply_markup=kb,
            )
            return

    # 2. EXTRACT METADATA
    status_msg = await message.answer("🔎 <i>Extracting video information...</i>")
    try:
        info = await YtDlpService.extract_metadata(canonical_url)
    except Exception as e:
        logger.error(f"Failed to extract metadata for {canonical_url}: {e}")
        await status_msg.edit_text("❌ Failed to retrieve video details. Please ensure the link is valid and public.")
        return

    # 3. DURATION VALIDATION (BEFORE QUEUE OR INTERACTION)
    is_valid, duration_secs, err_msg = YtDlpService.validate_duration(
        info, settings.MAX_VIDEO_DURATION_SECONDS, settings.ALLOW_UNKNOWN_DURATION
    )
    if not is_valid:
        await status_msg.edit_text(err_msg or "❌ This video exceeds the maximum duration limit.")
        return

    # 4. EXTRACT USABLE RESOLUTIONS
    available_heights = YtDlpService.get_available_resolutions(info)
    title = info.get("title", "YouTube Video")
    thumbnail_url = info.get("thumbnail")

    # Delete the temporary status message
    try:
        await status_msg.delete()
    except Exception:
        pass

    # 5. SEND THUMBNAIL PREVIEW WITH VIDEO TITLE AS CAPTION ONLY
    # YouTube description is strictly NEVER used as the caption!
    keyboard = build_quality_keyboard(source_id, available_heights)

    if thumbnail_url:
        try:
            await message.answer_photo(
                photo=thumbnail_url,
                caption=f"<b>{title}</b>",
                reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.warning(f"Could not send thumbnail photo: {e}. Falling back to text.")

    # Fallback to text if thumbnail fails
    await message.answer(
        text=f"🎬 <b>{title}</b>",
        reply_markup=keyboard,
    )
