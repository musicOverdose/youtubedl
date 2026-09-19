import uuid
from aiogram import Bot, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from src.bot.keyboards import build_must_join_keyboard, build_queue_status_keyboard, build_subtitle_keyboard
from src.core.config import settings
from src.core.constants import DeliveryStatus, JobStatus, OperationType
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.ai_service import AIService
from src.services.cache_service import CacheService
from src.services.must_join_service import MustJoinService
from src.services.queue_service import QueueService
from src.services.ytdlp_service import YtDlpService, get_canonical_url

logger = setup_logger("subtitle_handler")
subtitle_router = Router()


@subtitle_router.callback_query(lambda c: c.data and c.data.startswith("sub_menu:"))
async def on_subtitle_menu(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    source_id = callback.data.split(":")[1]
    canonical_url = get_canonical_url(source_id)

    async with AsyncSessionLocal() as session:
        # Must-Join check
        is_auth, missing_channels = await MustJoinService.require_must_join(
            bot, session, user_id, force_authoritative=True
        )
        if not is_auth:
            kb = build_must_join_keyboard(missing_channels)
            await callback.message.answer(
                "🔒 <b>You must join our channel(s) to continue:</b>", reply_markup=kb
            )
            await callback.answer()
            return

    try:
        info = await YtDlpService.extract_metadata(canonical_url)
    except Exception as e:
        logger.error(f"Error fetching metadata for subtitles: {e}")
        await callback.answer("Could not fetch subtitle options.", show_alert=True)
        return

    has_english, _, _ = YtDlpService.check_english_subtitles(info)
    if not has_english:
        await callback.answer(
            "No English subtitles are available for this video.", show_alert=True
        )
        return

    ai_ready = AIService.is_configured()
    keyboard = build_subtitle_keyboard(source_id, has_english=True, ai_available=ai_ready)
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"Error editing subtitle menu: {e}")

    await callback.answer()


@subtitle_router.callback_query(lambda c: c.data and c.data.startswith("sub:"))
async def on_subtitle_selected(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid parameters.", show_alert=True)
        return

    _, source_id, lang = parts
    if lang not in ("EN", "FA"):
        await callback.answer("Unsupported subtitle language.", show_alert=True)
        return

    canonical_url = get_canonical_url(source_id)

    async with AsyncSessionLocal() as session:
        # 1. MUST-JOIN AUTHORIZATION (AUTHORITATIVE)
        is_auth, missing_channels = await MustJoinService.require_must_join(
            bot, session, user_id, force_authoritative=True
        )
        if not is_auth:
            kb = build_must_join_keyboard(missing_channels)
            await callback.message.answer(
                "🔒 <b>You must join our channel(s) to download:</b>", reply_markup=kb
            )
            await callback.answer()
            return

        if lang == "FA" and not AIService.is_configured():
            await callback.answer(
                "Persian AI translation is currently not configured.", show_alert=True
            )
            return

        # 2. EXACT CACHE LOOKUP
        cache_key = CacheService.generate_cache_key(
            source_id=source_id,
            operation=OperationType.SUBTITLE.value,
            subtitle_lang=lang,
        )

        cached_entry = await CacheService.get_cached_entry(session, cache_key)
        if cached_entry:
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cached_entry, chat_id
            )
            if delivered:
                lang_name = "🇬🇧 English" if lang == "EN" else "🇮🇷 Persian"
                await callback.answer(f"Delivered {lang_name} subtitle from cache! 💬")
                return

        # 3. USER LIMITS
        allowed, limit_err = await QueueService.check_user_limits(session, user_id)
        if not allowed:
            await callback.answer(limit_err, show_alert=True)
            return

        # 4. DUPLICATE JOB COALESCING
        active_statuses = [
            JobStatus.QUEUED.value,
            JobStatus.PREPARING.value,
            JobStatus.DOWNLOADING.value,
            JobStatus.PROCESSING.value,
            JobStatus.UPLOADING.value,
        ]
        stmt = (
            select(Job)
            .where(Job.cache_key == cache_key, Job.status.in_(active_statuses))
            .order_by(Job.created_at.asc())
        )
        res = await session.execute(stmt)
        existing_job = res.scalars().first()

        lang_label = "🇬🇧 English Subtitle" if lang == "EN" else "🇮🇷 Persian AI Subtitle"

        if existing_job:
            req = JobRequest(
                job_id=existing_job.id,
                user_id=user_id,
                chat_id=chat_id,
                delivery_status=DeliveryStatus.PENDING.value,
            )
            session.add(req)
            await session.commit()

            pos = await QueueService.get_derived_position(existing_job.id)
            pos_str = f"#{pos}" if pos else "In Progress"
            active_count = len(await QueueService.get_active_job_ids())

            status_msg = await callback.message.answer(
                f"⏳ <b>Attached to existing job</b>\n"
                f"💬 <b>{lang_label}</b>\n"
                f"Position: {pos_str}\n"
                f"Active jobs: {active_count} / {settings.MAX_ACTIVE_JOBS}",
                reply_markup=build_queue_status_keyboard(existing_job.id),
            )
            req.status_message_id = status_msg.message_id
            await session.commit()
            await callback.answer("Added to queue!")
            return

        # 5. CREATE NEW SUBTITLE JOB
        try:
            info = await YtDlpService.extract_metadata(canonical_url)
            title = info.get("title", "Video Subtitles")
        except Exception:
            title = "Video Subtitles"

        job_id = str(uuid.uuid4())
        new_job = Job(
            id=job_id,
            source_url=canonical_url,
            canonical_url=canonical_url,
            source_id=source_id,
            title=title,
            operation=OperationType.SUBTITLE.value,
            subtitle_lang=lang,
            status=JobStatus.QUEUED.value,
            cache_key=cache_key,
        )
        session.add(new_job)

        req = JobRequest(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            delivery_status=DeliveryStatus.PENDING.value,
        )
        session.add(req)
        await session.commit()

        position = await QueueService.push_job(job_id)
        active_count = len(await QueueService.get_active_job_ids())

        status_msg = await callback.message.answer(
            f"⏳ <b>Added to queue</b>\n"
            f"💬 <b>{lang_label}</b>\n"
            f"Position: #{position}\n"
            f"Active: {active_count} / {settings.MAX_ACTIVE_JOBS}",
            reply_markup=build_queue_status_keyboard(job_id),
        )
        req.status_message_id = status_msg.message_id
        await session.commit()

    await callback.answer("Added to queue!")
