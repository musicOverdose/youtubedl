import uuid
from aiogram import Bot, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from src.bot.keyboards import build_must_join_keyboard, build_queue_status_keyboard
from src.core.config import settings
from src.core.constants import DeliveryStatus, JobStatus, OperationType
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.cache_service import CacheService
from src.services.must_join_service import MustJoinService
from src.services.queue_service import QueueService
from src.services.ytdlp_service import YtDlpService, get_canonical_url

logger = setup_logger("audio_handler")
audio_router = Router()


@audio_router.callback_query(lambda c: c.data and c.data.startswith("aud:"))
async def on_audio_selected(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid parameters.", show_alert=True)
        return

    _, source_id, format_type = parts
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

        # 2. EXACT CACHE LOOKUP
        cache_key = CacheService.generate_cache_key(
            source_id=source_id,
            operation=OperationType.AUDIO.value,
            codec="MP3",
        )

        cached_entry = await CacheService.get_cached_entry(session, cache_key)
        if cached_entry:
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cached_entry, chat_id
            )
            if delivered:
                await callback.answer("Delivered from cache! 🎵")
                return

        # 3. ENFORCE USER LIMITS
        allowed, limit_err = await QueueService.check_user_limits(session, user_id)
        if not allowed:
            await callback.answer(limit_err, show_alert=True)
            return

        # 4. GET TITLE
        try:
            info = await YtDlpService.extract_metadata(canonical_url)
            title = info.get("title", "Audio Track")
        except Exception:
            title = "Audio Track"

        # 5. DUPLICATE JOB COALESCING
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
                f"🎵 <b>MP3 Audio</b>\n"
                f"Position: {pos_str}\n"
                f"Active jobs: {active_count} / {settings.MAX_ACTIVE_JOBS}",
                reply_markup=build_queue_status_keyboard(existing_job.id),
            )
            req.status_message_id = status_msg.message_id
            await session.commit()
            await callback.answer("Added to queue!")
            return

        # 6. CREATE JOB & QUEUE
        job_id = str(uuid.uuid4())
        new_job = Job(
            id=job_id,
            source_url=canonical_url,
            canonical_url=canonical_url,
            source_id=source_id,
            title=title,
            operation=OperationType.AUDIO.value,
            output_codec="MP3",
            resolution=None,
            target_height=None,
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
            f"🎵 <b>MP3 Audio</b>\n"
            f"Position: #{position}\n"
            f"Active: {active_count} / {settings.MAX_ACTIVE_JOBS}",
            reply_markup=build_queue_status_keyboard(job_id),
        )
        req.status_message_id = status_msg.message_id
        await session.commit()

    await callback.answer("Added to queue!")
