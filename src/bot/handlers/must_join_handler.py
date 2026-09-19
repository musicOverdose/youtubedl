from datetime import datetime, timezone
from aiogram import Bot, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select, update
from src.bot.keyboards import build_must_join_keyboard
from src.core.constants import DeliveryStatus
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.cache import CacheEntry
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.cache_service import CacheService
from src.services.must_join_service import MustJoinService

logger = setup_logger("must_join_handler")
must_join_router = Router()


@must_join_router.callback_query(lambda c: c.data and c.data.startswith("mj_chk"))
async def on_must_join_check_again(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    data = callback.data

    resume_url = None
    if ":" in data:
        resume_url = data.split(":", 1)[1]

    async with AsyncSessionLocal() as session:
        # ALWAYS FRESH AUTHORITATIVE TELEGRAM API CHECK!
        is_auth, missing_channels = await MustJoinService.require_must_join(
            bot, session, user_id, force_authoritative=True
        )

        if not is_auth:
            kb = build_must_join_keyboard(missing_channels, resume_action=resume_url)
            await callback.answer(
                "❌ You still haven't joined all required channels. Please join them first!",
                show_alert=True,
            )
            try:
                await callback.message.edit_reply_markup(reply_markup=kb)
            except Exception:
                pass
            return

        # User is AUTHORIZED!
        await callback.answer("✅ Membership verified! Thank you.", show_alert=False)
        try:
            await callback.message.edit_text("✅ <b>Membership verified successfully!</b>")
        except Exception:
            pass

        # 1. CHECK FOR PENDING 'WAITING_FOR_AUTHORIZATION' DELIVERIES
        # If user left during download but rejoined, deliver now from cache without re-download!
        stmt = (
            select(JobRequest, Job, CacheEntry)
            .join(Job, Job.id == JobRequest.job_id)
            .join(CacheEntry, CacheEntry.cache_key == Job.cache_key)
            .where(
                JobRequest.user_id == user_id,
                JobRequest.delivery_status == DeliveryStatus.WAITING_FOR_AUTHORIZATION.value,
                CacheEntry.is_valid == True,
            )
        )
        res = await session.execute(stmt)
        pending_items = res.all()

        for req, job, cache_entry in pending_items:
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cache_entry, chat_id
            )
            if delivered:
                await session.execute(
                    update(JobRequest)
                    .where(JobRequest.id == req.id)
                    .values(
                        delivery_status=DeliveryStatus.DELIVERED.value,
                        delivered_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                logger.info(f"Delivered previously pending file to rejoined user {user_id}")

        if resume_url:
            await callback.message.answer(
                f"🔗 Resuming your request for:\n{resume_url}\nPlease send the link again or choose an option above."
            )
        else:
            await callback.message.answer(
                "🎉 You are all set! Send any YouTube link to start downloading."
            )
