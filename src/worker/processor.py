import asyncio
from datetime import datetime, timezone
import glob
import os
import shutil
from typing import Dict, List, Optional
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import yt_dlp
from src.bot.keyboards import build_must_join_keyboard
from src.core.config import settings
from src.core.constants import DeliveryStatus, JobStatus, OperationType
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.ai_service import AIService
from src.services.cache_service import CacheService
from src.services.ffmpeg_service import FFmpegService
from src.services.must_join_service import MustJoinService
from src.services.queue_service import QueueService
from src.services.system_service import SystemService
from src.services.ytdlp_service import YtDlpService
from src.worker.notifier import StatusNotifier

logger = setup_logger("worker_processor")


class JobProcessor:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def process_job(self, job_id: str) -> None:
        """
        Executes a single media processing pipeline.
        Guarantees cleanup, concurrency release, and cache-consistency.
        """
        job_dir = os.path.join(settings.TEMP_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        async with AsyncSessionLocal() as session:
            # 1. Fetch Job and active requests
            stmt = select(Job).where(Job.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()

            if not job:
                logger.error(f"Job {job_id} not found in database")
                await QueueService.release_active_job(job_id)
                shutil.rmtree(job_dir, ignore_errors=True)
                return

            req_stmt = select(JobRequest).where(JobRequest.job_id == job_id)
            req_res = await session.execute(req_stmt)
            job_requests = list(req_res.scalars().all())

            subscribers = [
                {"chat_id": r.chat_id, "message_id": r.status_message_id}
                for r in job_requests
            ]
            notifier = StatusNotifier(self.bot, subscribers)

            # Check pre-flight disk safety
            safe_disk, disk_err = SystemService.check_disk_safety()
            if not safe_disk:
                logger.error(f"Disk protection triggered: {disk_err}")
                job.status = JobStatus.FAILED.value
                job.error_code = "DISK_LIMIT"
                job.error_message = disk_err
                await session.commit()
                await notifier.update("❌", "Failed", disk_err, force=True)
                await QueueService.release_active_job(job_id)
                shutil.rmtree(job_dir, ignore_errors=True)
                return

            # Update job status to PREPARING
            job.status = JobStatus.PREPARING.value
            job.started_at = datetime.now(timezone.utc)
            await session.commit()
            await notifier.update("🔎", "Preparing", "Initializing download parameters...", force=True)

            try:
                # 2. Check for cancellation
                if await QueueService.is_cancelled(job_id):
                    logger.info(f"Job {job_id} was cancelled before starting")
                    job.status = JobStatus.CANCELLED.value
                    await session.commit()
                    return

                uploaded_msg_id = None
                cache_channel_id = settings.TELEGRAM_CACHE_CHANNEL_ID
                if not cache_channel_id:
                    raise ValueError("TELEGRAM_CACHE_CHANNEL_ID is not configured!")

                # ==========================================
                # PIPELINE: VIDEO PROCESSING
                # ==========================================
                if job.operation == OperationType.VIDEO.value or job.operation == "VIDEO":
                    target_height = job.target_height or 1080
                    target_codec = job.output_codec or "H264"

                    # Download with yt-dlp
                    job.status = JobStatus.DOWNLOADING.value
                    await session.commit()
                    await notifier.update("⬇️", "Downloading", f"Resolution: {job.resolution}", force=True)

                    download_template = os.path.join(job_dir, "input.%(ext)s")
                    ydl_opts = YtDlpService.get_base_opts()
                    ydl_opts.update({
                        "outtmpl": download_template,
                        "format": YtDlpService.build_video_format_spec(target_height),
                    })

                    def progress_hook(d):
                        if d.get("status") == "downloading":
                            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                            downloaded = d.get("downloaded_bytes", 0)
                            pct = (downloaded / total) * 100
                            speed = d.get("_speed_str", "")
                            eta = d.get("_eta_str", "")
                            asyncio.run_coroutine_threadsafe(
                                notifier.update("⬇️", "Downloading", f"Progress: {pct:.1f}% | Speed: {speed} | ETA: {eta}"),
                                asyncio.get_event_loop(),
                            )

                    ydl_opts["progress_hooks"] = [progress_hook]

                    loop = asyncio.get_running_loop()
                    def _dl():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([job.canonical_url])
                    await loop.run_in_executor(None, _dl)

                    if await QueueService.is_cancelled(job_id):
                        job.status = JobStatus.CANCELLED.value
                        await session.commit()
                        return

                    # Find downloaded file
                    input_files = glob.glob(os.path.join(job_dir, "input.*"))
                    if not input_files:
                        raise FileNotFoundError("Downloaded video file not found")
                    input_file = input_files[0]

                    # FFmpeg stage
                    job.status = JobStatus.PROCESSING.value
                    await session.commit()
                    await notifier.update("⚙️", "Processing", f"Applying {target_codec} container...", force=True)

                    output_file = os.path.join(job_dir, "output.mp4")
                    process_ok = await FFmpegService.process_video(
                        input_path=input_file,
                        output_path=output_file,
                        target_codec=target_codec,
                        target_height=target_height,
                    )
                    if not process_ok or not os.path.exists(output_file):
                        raise RuntimeError("FFmpeg processing failed")

                    # Upload to Cache Channel
                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    await notifier.update("☁️", "Uploading", "Sending media to secure cache...", force=True)

                    tg_file = FSInputFile(output_file, filename=f"{job.title[:60]}.mp4")
                    caption_text = f"🎬 <b>{job.title}</b>\n({target_codec} {job.resolution})"

                    upload_msg = await self.bot.send_video(
                        chat_id=cache_channel_id,
                        video=tg_file,
                        caption=caption_text,
                        supports_streaming=True,
                    )
                    uploaded_msg_id = upload_msg.message_id
                    file_size = os.path.getsize(output_file)

                    # Save Cache Entry in DB
                    cache_entry = await CacheService.save_cache_entry(
                        session=session,
                        cache_key=job.cache_key,
                        source_id=job.source_id,
                        title=job.title,
                        operation=job.operation,
                        codec=target_codec,
                        resolution=job.resolution,
                        height=target_height,
                        file_size=file_size,
                        telegram_channel_id=cache_channel_id,
                        telegram_message_id=uploaded_msg_id,
                    )

                # ==========================================
                # PIPELINE: AUDIO (MP3) PROCESSING
                # ==========================================
                elif job.operation == OperationType.AUDIO.value or job.operation == "AUDIO":
                    job.status = JobStatus.DOWNLOADING.value
                    await session.commit()
                    await notifier.update("⬇️", "Downloading", "Fetching high quality audio...", force=True)

                    audio_template = os.path.join(job_dir, "audio.%(ext)s")
                    ydl_opts = YtDlpService.get_base_opts()
                    ydl_opts.update({
                        "outtmpl": audio_template,
                        "format": "bestaudio/best",
                        "writethumbnail": True,
                    })

                    loop = asyncio.get_running_loop()
                    def _dl_audio():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([job.canonical_url])
                    await loop.run_in_executor(None, _dl_audio)

                    audio_files = [f for f in glob.glob(os.path.join(job_dir, "audio.*")) if not f.endswith((".jpg", ".webp", ".png"))]
                    if not audio_files:
                        raise FileNotFoundError("Downloaded audio file not found")
                    input_audio = audio_files[0]

                    thumb_files = glob.glob(os.path.join(job_dir, "audio.*.jpg")) or glob.glob(os.path.join(job_dir, "audio.*.webp"))
                    cover_path = thumb_files[0] if thumb_files else None

                    # FFmpeg MP3 stage
                    job.status = JobStatus.PROCESSING.value
                    await session.commit()
                    await notifier.update("⚙️", "Processing", "Converting to MP3 with ID3 tags...", force=True)

                    output_mp3 = os.path.join(job_dir, "output.mp3")
                    mp3_ok = await FFmpegService.extract_mp3(
                        input_audio_path=input_audio,
                        output_mp3_path=output_mp3,
                        title=job.title,
                        cover_image_path=cover_path,
                    )
                    if not mp3_ok or not os.path.exists(output_mp3):
                        raise RuntimeError("MP3 conversion failed")

                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    await notifier.update("☁️", "Uploading", "Sending audio to secure cache...", force=True)

                    tg_audio = FSInputFile(output_mp3, filename=f"{job.title[:60]}.mp3")
                    upload_msg = await self.bot.send_audio(
                        chat_id=cache_channel_id,
                        audio=tg_audio,
                        title=job.title,
                    )
                    uploaded_msg_id = upload_msg.message_id
                    file_size = os.path.getsize(output_mp3)

                    cache_entry = await CacheService.save_cache_entry(
                        session=session,
                        cache_key=job.cache_key,
                        source_id=job.source_id,
                        title=job.title,
                        operation=job.operation,
                        codec="MP3",
                        file_size=file_size,
                        telegram_channel_id=cache_channel_id,
                        telegram_message_id=uploaded_msg_id,
                    )

                # ==========================================
                # PIPELINE: SUBTITLE PROCESSING
                # ==========================================
                elif job.operation == OperationType.SUBTITLE.value or job.operation == "SUBTITLE":
                    job.status = JobStatus.DOWNLOADING.value
                    await session.commit()
                    await notifier.update("⬇️", "Downloading", "Extracting English subtitles...", force=True)

                    sub_template = os.path.join(job_dir, "subs.%(ext)s")
                    ydl_opts = YtDlpService.get_base_opts()
                    ydl_opts.update({
                        "outtmpl": sub_template,
                        "skip_download": True,
                        "writesubtitles": True,
                        "writeautomaticsub": True,
                        "subtitleslangs": ["en.*", "en"],
                        "subtitlesformat": "srt/vtt/best",
                    })

                    loop = asyncio.get_running_loop()
                    def _dl_subs():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([job.canonical_url])
                    await loop.run_in_executor(None, _dl_subs)

                    sub_files = glob.glob(os.path.join(job_dir, "subs*.*"))
                    if not sub_files:
                        raise FileNotFoundError("English subtitles could not be extracted")

                    with open(sub_files[0], "r", encoding="utf-8", errors="ignore") as sf:
                        raw_sub_content = sf.read()

                    # Convert / Normalize to clean SRT
                    parsed_segs = AIService.parse_srt(raw_sub_content)
                    if not parsed_segs:
                        raise ValueError("Failed to parse subtitle segments")
                    english_srt = "\n".join(seg.to_srt() for seg in parsed_segs)

                    final_srt_content = english_srt
                    target_lang = job.subtitle_lang or "EN"

                    if target_lang == "FA":
                        job.status = JobStatus.PROCESSING.value
                        await session.commit()
                        await notifier.update("⚙️", "Translating", "Translating subtitles to Persian with AI...", force=True)

                        ok, persian_srt, ai_err = await AIService.translate_english_to_persian(english_srt)
                        if not ok or not persian_srt:
                            raise RuntimeError(f"Persian translation failed: {ai_err}")
                        final_srt_content = persian_srt

                    out_srt_file = os.path.join(job_dir, f"{target_lang}_subtitles.srt")
                    with open(out_srt_file, "w", encoding="utf-8") as f:
                        f.write(final_srt_content)

                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    await notifier.update("☁️", "Uploading", "Sending subtitles to cache...", force=True)

                    tg_sub = FSInputFile(out_srt_file, filename=f"{job.title[:50]}_{target_lang}.srt")
                    upload_msg = await self.bot.send_document(
                        chat_id=cache_channel_id,
                        document=tg_sub,
                        caption=f"💬 <b>{job.title}</b> ({target_lang} Subtitle)",
                    )
                    uploaded_msg_id = upload_msg.message_id
                    file_size = os.path.getsize(out_srt_file)

                    cache_entry = await CacheService.save_cache_entry(
                        session=session,
                        cache_key=job.cache_key,
                        source_id=job.source_id,
                        title=job.title,
                        operation=job.operation,
                        subtitle_lang=target_lang,
                        file_size=file_size,
                        telegram_channel_id=cache_channel_id,
                        telegram_message_id=uploaded_msg_id,
                    )

                # ==========================================
                # FINAL DELIVERY TO SUBSCRIBERS
                # STRICT MUST-JOIN RE-CHECK PER USER
                # ==========================================
                job.status = JobStatus.COMPLETED.value
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()

                # Refresh request list
                fresh_req_res = await session.execute(
                    select(JobRequest).where(JobRequest.job_id == job_id)
                )
                current_requests = list(fresh_req_res.scalars().all())

                for req in current_requests:
                    # RE-CHECK MUST-JOIN AUTHORITATIVELY FOR EACH SUBSCRIBER
                    is_auth, missing_channels = await MustJoinService.require_must_join(
                        self.bot, session, req.user_id, force_authoritative=True
                    )

                    if not is_auth:
                        # User left required channel during processing!
                        # Mark as WAITING_FOR_AUTHORIZATION and do not deliver
                        logger.warning(
                            f"User {req.user_id} left channel before delivery. Setting WAITING_FOR_AUTHORIZATION."
                        )
                        req.delivery_status = DeliveryStatus.WAITING_FOR_AUTHORIZATION.value
                        await session.commit()

                        kb = build_must_join_keyboard(missing_channels)
                        try:
                            await self.bot.send_message(
                                chat_id=req.chat_id,
                                text=(
                                    "🔒 <b>Your file is ready!</b>\n"
                                    "However, you left one of the required channels. "
                                    "Please re-join the channels below and tap <b>Check Again</b> to receive your file."
                                ),
                                reply_markup=kb,
                            )
                        except Exception:
                            pass
                    else:
                        # User is authorized: deliver via copyMessage!
                        delivered, err = await CacheService.deliver_cached_media(
                            self.bot, session, cache_entry, req.chat_id
                        )
                        if delivered:
                            req.delivery_status = DeliveryStatus.DELIVERED.value
                            req.delivered_at = datetime.now(timezone.utc)
                            await session.commit()

                            # Edit status message to complete
                            if req.status_message_id:
                                try:
                                    await self.bot.edit_message_text(
                                        chat_id=req.chat_id,
                                        message_id=req.status_message_id,
                                        text="✅ <b>Download complete! Delivered above.</b>",
                                    )
                                except Exception:
                                    pass
                        else:
                            req.delivery_status = DeliveryStatus.FAILED.value
                            await session.commit()

            except Exception as e:
                logger.error(f"Error executing job {job_id}: {e}", exc_info=True)
                job.status = JobStatus.FAILED.value
                job.error_code = "PROCESSING_ERROR"
                job.error_message = str(e)[:500]
                await session.commit()
                await notifier.update("❌", "Failed", f"Error: {str(e)[:120]}", force=True)

            finally:
                # GUARANTEED CLEANUP:
                # 1. Delete local temporary files
                shutil.rmtree(job_dir, ignore_errors=True)
                logger.info(f"Cleaned temp directory for job {job_id}")

                # 2. Release active slot in Redis
                await QueueService.release_active_job(job_id)

                # 3. Release cache lock
                await QueueService.release_cache_lock(job.cache_key)
