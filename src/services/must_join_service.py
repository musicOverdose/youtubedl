from datetime import datetime, timezone
from typing import List, Optional, Tuple
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import ChatMember
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import MEMBERSHIP_CACHE_TTL, REDIS_KEY_MEMBERSHIP_PREFIX
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.channel import RequiredChannel

logger = setup_logger("must_join_service")


def is_active_member(chat_member: ChatMember) -> bool:
    """
    Authoritative evaluation of Telegram membership state:
    - Authorized: 'creator', 'administrator', 'member', and 'restricted' with is_member=True.
    - Unauthorized: 'left', 'kicked', and 'restricted' with is_member=False.
    """
    status = chat_member.status
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        # Check explicit is_member attribute on ChatMemberRestricted
        return getattr(chat_member, "is_member", False) is True
    # 'left', 'kicked', etc.
    return False


class MustJoinService:
    @staticmethod
    async def get_enabled_channels(session: AsyncSession) -> List[RequiredChannel]:
        """Fetches all active required channels from database."""
        stmt = select(RequiredChannel).where(RequiredChannel.enabled == True)
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @classmethod
    async def check_channel_membership_authoritative(
        cls, bot: Bot, chat_id: int, user_id: int
    ) -> bool:
        """
        Performs a fresh, authoritative Telegram API getChatMember request.
        """
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            return is_active_member(member)
        except TelegramBadRequest as e:
            logger.warning(f"getChatMember failed for chat {chat_id}, user {user_id}: {e}")
            return False
        except TelegramAPIError as e:
            logger.error(f"Telegram API error checking membership: {e}")
            return False

    @classmethod
    async def require_must_join(
        cls,
        bot: Bot,
        session: AsyncSession,
        user_id: int,
        force_authoritative: bool = True,
    ) -> Tuple[bool, List[RequiredChannel]]:
        """
        Centralized server-side guard.
        Checks ALL enabled required channels.
        Returns: (is_authorized, missing_channels)
        """
        # If globally disabled, immediately permit
        if not settings.MUST_JOIN_ENABLED:
            return True, []

        channels = await cls.get_enabled_channels(session)
        if not channels:
            return True, []

        missing_channels = []

        for ch in channels:
            is_member = False
            # Check fast UI cache ONLY if not forced authoritative
            if not force_authoritative:
                r = get_redis_client()
                cache_key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}{ch.chat_id}:{user_id}"
                cached = await r.get(cache_key)
                if cached == "1":
                    is_member = True
                elif cached == "0":
                    is_member = False
                else:
                    is_member = await cls.check_channel_membership_authoritative(
                        bot, ch.chat_id, user_id
                    )
            else:
                # Always authoritative for security decisions
                is_member = await cls.check_channel_membership_authoritative(
                    bot, ch.chat_id, user_id
                )
                # Store fast hint
                r = get_redis_client()
                cache_key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}{ch.chat_id}:{user_id}"
                await r.set(cache_key, "1" if is_member else "0", ex=MEMBERSHIP_CACHE_TTL)

            if not is_member:
                missing_channels.append(ch)

        is_authorized = len(missing_channels) == 0
        return is_authorized, missing_channels

    @staticmethod
    async def verify_bot_is_admin(bot: Bot, chat_id: int) -> Tuple[bool, str]:
        """Verifies that the bot is an administrator in the given channel."""
        try:
            bot_info = await bot.get_me()
            chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_info.id)
            if chat_member.status in ("administrator", "creator"):
                return True, "Bot is administrator"
            return False, "Bot is not an administrator in this channel"
        except TelegramBadRequest as e:
            return False, f"Cannot access channel: {e}"
        except TelegramAPIError as e:
            return False, f"Telegram error: {e}"
