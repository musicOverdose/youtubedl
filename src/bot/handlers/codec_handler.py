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

logger = setup_logger("codec_handler")
codec_router = Router()


@codec_router.callback_query(lambda c: c.data and c.data.startswith("c:"))
async def on_codec_selected(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Invalid parameters.", show_alert=True)
        return

    _, source_id, codec, height_str = parts
    try:
        height = int(height_str)
    except ValueError:
        await callback.answer("Invalid height.", show_alert=True)
        return

    if codec not in ("H264", "H265"):
        await callback.answer("Unsupported video codec.", show_alert=True)
        return

    canonical_url = get_canonical_url(source_id)

    async with AsyncSessionLocal() as session:
        # 1. MUST-JOIN AUTHORIZATION (AUTHORITATIVE)
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

        # 2. EXACT QUALITY & CODEC VALIDATION & STALE BUTTON CHECK
        try:
            info = await YtDlpService.extract_metadata(canonical_url)
            available_heights = YtDlpService.get_available_resolutions(info)
            if height not in available_heights:
                await callback.answer(
                    f"❌ Resolution {height}p is no longer available. Please choose from current qualities.",
                    show_alert=True,
                )
                return

            available_codecs = YtDlpService.get_available_codecs_for_height(info, height)
            if codec not in available_codecs:
                await callback.answer(
                    f"❌ Codec {codec} is not available for {height}p from YouTube.",
                    show_alert=True,
                )
                return

            title = info.get("title", "YouTube Video")
        except Exception as e:
            logger.error(f"Error fetching metadata for verification: {e}")
            title = "YouTube Video"

        # 3. EXACT CACHE KEY GENERATION & CACHE LOOKUP
        cache_key = CacheService.generate_cache_key(
            source_id=source_id,
            operation=OperationType.VIDEO.value,
            codec=codec,
            height=height,
        )

        cached_entry = await CacheService.get_cached_entry(session, cache_key)
        if cached_entry:
            # CACHE HIT: Instant delivery via copyMessage!
            # Bypasses queue, worker, yt-dlp, FFmpeg, and temp disk
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cached_entry, chat_id
            )
            if delivered:
                await callback.answer("Delivered from cache! 🚀")
                return
            else:
                logger.warning(f"Cache delivery failed: {err}. Falling through to download.")

        # 4. ENFORCE PER-USER LIMITS
        allowed, limit_err = await QueueService.check_user_limits(session, user_id)
        if not allowed:
            await callback.answer(limit_err, show_alert=True)
            return

        # 5. DUPLICATE JOB COALESCING
        # Check if an active/queued job for this cache_key already exists
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
            # Coalesce: Attach this user request to existing job
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
                f"🎬 <b>{codec} · {height}p</b>\n"
                f"Position: {pos_str}\n"
                f"Active jobs: {active_count} / {settings.MAX_ACTIVE_JOBS}",
                reply_markup=build_queue_status_keyboard(existing_job.id),
            )
            req.status_message_id = status_msg.message_id
            await session.commit()
            await callback.answer("Added to queue!")
            return

        # 6. CREATE NEW JOB & QUEUE
        job_id = str(uuid.uuid4())
        new_job = Job(
            id=job_id,
            source_url=canonical_url,
            canonical_url=canonical_url,
            source_id=source_id,
            title=title,
            operation=OperationType.VIDEO.value,
            output_codec=codec,
            resolution=f"{height}p",
            target_height=height,
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

        # Push to persistent Redis FIFO queue
        position = await QueueService.push_job(job_id)
        active_count = len(await QueueService.get_active_job_ids())

        status_msg = await callback.message.answer(
            f"⏳ <b>Added to queue</b>\n"
            f"🎬 <b>{codec} · {height}p</b>\n"
            f"Position: #{position}\n"
            f"Active: {active_count} / {settings.MAX_ACTIVE_JOBS}",
            reply_markup=build_queue_status_keyboard(job_id),
        )
        req.status_message_id = status_msg.message_id
        await session.commit()

    await callback.answer("Added to queue!")
