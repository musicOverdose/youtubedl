from aiogram import Bot, Router
from aiogram.types import CallbackQuery
from src.bot.keyboards import build_codec_keyboard, build_must_join_keyboard, build_quality_keyboard
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.must_join_service import MustJoinService
from src.services.ytdlp_service import YtDlpService, get_canonical_url

logger = setup_logger("quality_handler")
quality_router = Router()


@quality_router.callback_query(lambda c: c.data and c.data.startswith("q:"))
async def on_quality_selected(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid request.", show_alert=True)
        return

    _, source_id, height_str = parts
    try:
        height = int(height_str)
    except ValueError:
        await callback.answer("Invalid resolution.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    # Second step: switch to Codec Selection on the SAME message
    codec_keyboard = build_codec_keyboard(source_id, height)
    try:
        await callback.message.edit_reply_markup(reply_markup=codec_keyboard)
    except Exception as e:
        logger.warning(f"Error updating keyboard to codec selection: {e}")

    await callback.answer()


@quality_router.callback_query(lambda c: c.data and c.data.startswith("back_q:"))
async def on_back_to_quality(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    source_id = callback.data.split(":")[1]
    canonical_url = get_canonical_url(source_id)

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    try:
        info = await YtDlpService.extract_metadata(canonical_url)
        available_heights = YtDlpService.get_available_resolutions(info)
        keyboard = build_quality_keyboard(source_id, available_heights)
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error going back to quality menu: {e}")
        await callback.answer("Could not refresh quality options.", show_alert=True)
        return

    await callback.answer()
