import asyncio
from datetime import datetime, timezone
import glob
import os
from pathlib import Path
import shutil
from typing import Dict, List, Optional, Tuple
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
from src.core.redis import get_redis_client
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
from src.worker.telegram_factory import TelegramClientFactory

logger = setup_logger("worker_processor")


class JobProcessor:
    def __init__(self, bot: Optional[Bot] = None):
        self.bot = bot

    async def report_telemetry(self) -> None:
        """Periodically publishes Worker disk usage telemetry to Redis."""
        try:
            r = get_redis_client()
            temp_bytes = 0
            if os.path.exists(settings.TEMP_DIR):
                for root, _, files in os.walk(settings.TEMP_DIR):
                    for f in files:
                        fp = os.path.join(root, f)
                        if os.path.exists(fp) and not os.path.islink(fp):
                            temp_bytes += os.path.getsize(fp)

            transfer_bytes = 0
            if os.path.exists(settings.TRANSFER_DIR):
                for root, _, files in os.walk(settings.TRANSFER_DIR):
                    for f in files:
                        fp = os.path.join(root, f)
                        if os.path.exists(fp) and not os.path.islink(fp):
                            transfer_bytes += os.path.getsize(fp)

            disk_usage = shutil.disk_usage(settings.TEMP_DIR)

            await r.set("telemetry:worker:temp_ytdl_bytes", str(temp_bytes))
            await r.set("telemetry:worker:transfer_bytes", str(transfer_bytes))
            await r.set("telemetry:host:filesystem_free_bytes", str(disk_usage.free))
            await r.set("telemetry:host:filesystem_total_bytes", str(disk_usage.total))
            await r.set("telemetry:host:filesystem_used_bytes", str(disk_usage.used))
        except Exception as e:
            logger.debug("Failed to report telemetry to Redis: %s", e)

    async def _send_media_to_cache(
        self,
        bot: Bot,
        api_mode: str,
        channel_id: int,
        local_file_path: str,
        operation: str,
        job_id: str,
        job_title: str,
        caption: str = "",
        extra_kwargs: Optional[dict] = None,
    ) -> Tuple[int, int]:
        """
        Mode-branched media upload handler:
        - Local Mode: Stages file to /transfer/<job-id>/<filename> with 0755/0644 permissions,
          passes string URI (file:///transfer/...) to Local Bot API (zero-multipart local handoff),
          and cleans up immediately post-upload.
        - Cloud Mode: Preflights 50 MB limit, streams multipart via FSInputFile.
        """
        extra_kwargs = extra_kwargs or {}
        file_size = os.path.getsize(local_file_path)
        transfer_job_dir = Path(settings.TRANSFER_DIR) / job_id

        if api_mode == "cloud":
            # 50 MB preflight check
            if file_size > 50 * 1024 * 1024:
                size_mb = round(file_size / (1024 * 1024), 1)
                raise ValueError(
                    f"File size ({size_mb} MB) exceeds Cloud Bot API limit of 50 MB. "
                    "Switch to Local Bot API in Web Admin for up to 2000 MB uploads."
                )

            # Upload via FSInputFile (multipart)
            if operation == OperationType.VIDEO.value or operation == "VIDEO":
                tg_file = FSInputFile(local_file_path, filename=f"{job_title[:60]}.mp4")
                msg = await bot.send_video(
                    chat_id=channel_id,
                    video=tg_file,
                    caption=caption,
                    **extra_kwargs,
                )
            elif operation == OperationType.AUDIO.value or operation == "AUDIO":
                tg_file = FSInputFile(local_file_path, filename=f"{job_title[:60]}.mp3")
                msg = await bot.send_audio(
                    chat_id=channel_id,
                    audio=tg_file,
                    title=job_title,
                    **extra_kwargs,
                )
            else:
                tg_file = FSInputFile(local_file_path, filename=f"{job_title[:50]}.srt")
                msg = await bot.send_document(
                    chat_id=channel_id,
                    document=tg_file,
                    caption=caption,
                    **extra_kwargs,
                )
            return msg.message_id, file_size

        else:
            # Local Bot API Mode: Zero-multipart local file handoff
            try:
                transfer_job_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(transfer_job_dir, 0o755)
            except OSError as e:
                logger.debug("Failed to set chmod 0755 on %s: %s", transfer_job_dir, e)

            staged_file = transfer_job_dir / Path(local_file_path).name
            shutil.copy2(local_file_path, staged_file)
            try:
                os.chmod(staged_file, 0o644)
            except OSError as e:
                logger.debug("Failed to set chmod 0644 on %s: %s", staged_file, e)

            file_uri = staged_file.resolve().as_uri()
            logger.info("Local Bot API handoff via URI: %s", file_uri)

            try:
                if operation == OperationType.VIDEO.value or operation == "VIDEO":
                    msg = await bot.send_video(
                        chat_id=channel_id,
                        video=file_uri,
                        caption=caption,
                        **extra_kwargs,
                    )
                elif operation == OperationType.AUDIO.value or operation == "AUDIO":
                    msg = await bot.send_audio(
                        chat_id=channel_id,
                        audio=file_uri,
                        title=job_title,
                        **extra_kwargs,
                    )
                else:
                    msg = await bot.send_document(
                        chat_id=channel_id,
                        document=file_uri,
                        caption=caption,
                        **extra_kwargs,
                    )
                return msg.message_id, file_size
            finally:
                # Immediate cleanup of transfer directory
                shutil.rmtree(transfer_job_dir, ignore_errors=True)

    async def process_job(self, job_id: str) -> None:
        """
        Executes a single media processing pipeline.
        Guarantees cleanup, concurrency release, and cache-consistency.
        """
        job_dir = os.path.join(settings.TEMP_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        transfer_job_dir = Path(settings.TRANSFER_DIR) / job_id

        # Resolve active bot and mode dynamically
        if self.bot is not None:
            bot = self.bot
            try:
                r = get_redis_client()
                m = await r.get("telegram:active:mode")
                api_mode = m.decode("utf-8") if isinstance(m, bytes) else str(m) if m else (settings.TELEGRAM_API_MODE or "local")
            except Exception:
                api_mode = settings.TELEGRAM_API_MODE or "local"
        else:
            bot, api_mode = await TelegramClientFactory.get_client()

        async with AsyncSessionLocal() as session:
            # 1. Fetch Job and active requests
            stmt = select(Job).where(Job.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()

            if not job:
                logger.error(f"Job {job_id} not found in database")
                await QueueService.release_active_job(job_id)
                shutil.rmtree(job_dir, ignore_errors=True)
                shutil.rmtree(transfer_job_dir, ignore_errors=True)
                return

            req_stmt = select(JobRequest).where(JobRequest.job_id == job_id)
            req_res = await session.execute(req_stmt)
            job_requests = list(req_res.scalars().all())

            subscribers = [
                {"chat_id": r.chat_id, "message_id": r.status_message_id}
                for r in job_requests
            ]
            notifier = StatusNotifier(bot, subscribers)

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
                shutil.rmtree(transfer_job_dir, ignore_errors=True)
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
                # Also attempt to check Redis for dynamic cache channel ID
                try:
                    r = get_redis_client()
                    c_chan = await r.get("telegram:active:cache_channel_id")
                    if c_chan:
                        val = c_chan.decode("utf-8") if isinstance(c_chan, bytes) else str(c_chan)
                        cache_channel_id = int(val)
                except Exception:
                    pass

                if not cache_channel_id:
                    raise ValueError("TELEGRAM_CACHE_CHANNEL_ID is not configured!")

                # ==========================================
                # PIPELINE: VIDEO PROCESSING
                # ==========================================
                if job.operation == OperationType.VIDEO.value or job.operation == "VIDEO":
                    target_height = job.target_height or 1080
                    target_codec = job.output_codec or "H264"

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

                    input_files = glob.glob(os.path.join(job_dir, "input.*"))
                    if not input_files:
                        raise FileNotFoundError("Downloaded video file not found")
                    input_file = input_files[0]

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

                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    await notifier.update("☁️", "Uploading", "Sending media to secure cache...", force=True)

                    caption_text = f"🎬 <b>{job.title}</b>\n({target_codec} {job.resolution})"
                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=output_file,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=job.title,
                        caption=caption_text,
                        extra_kwargs={"supports_streaming": True},
                    )

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

                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=output_mp3,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=job.title,
                    )

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

                    caption_text = f"💬 <b>{job.title}</b> ({target_lang} Subtitle)"
                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=out_srt_file,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=f"{job.title[:50]}_{target_lang}",
                        caption=caption_text,
                    )

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
                    is_auth, missing_channels = await MustJoinService.require_must_join(
                        bot, session, req.user_id, force_authoritative=True
                    )

                    if not is_auth:
                        logger.warning(
                            f"User {req.user_id} left channel before delivery. Setting WAITING_FOR_AUTHORIZATION."
                        )
                        req.delivery_status = DeliveryStatus.WAITING_FOR_AUTHORIZATION.value
                        await session.commit()

                        kb = build_must_join_keyboard(missing_channels)
                        try:
                            await bot.send_message(
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
                        delivered, err = await CacheService.deliver_cached_media(
                            bot, session, cache_entry, req.chat_id
                        )
                        if delivered:
                            req.delivery_status = DeliveryStatus.DELIVERED.value
                            req.delivered_at = datetime.now(timezone.utc)
                            await session.commit()

                            if req.status_message_id:
                                try:
                                    await bot.edit_message_text(
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
                shutil.rmtree(job_dir, ignore_errors=True)
                shutil.rmtree(transfer_job_dir, ignore_errors=True)
                logger.info(f"Cleaned directories for job {job_id}")

                await QueueService.release_active_job(job_id)
                await QueueService.release_cache_lock(job.cache_key)

                # Report disk telemetry
                await self.report_telemetry()
