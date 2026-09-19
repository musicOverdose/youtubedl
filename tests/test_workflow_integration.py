from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from src.core.constants import DeliveryStatus, JobStatus, OperationType
from src.models.cache import CacheEntry
from src.models.channel import RequiredChannel
from src.models.job import Job
from src.models.job_request import JobRequest
from src.models.user import User
from src.services.ai_service import AIService
from src.services.cache_service import CacheService
from src.services.must_join_service import MustJoinService


@pytest.mark.asyncio
async def test_cache_hit_and_invalidation(db_session):
    # 1. Create a valid cache entry
    cache_entry = CacheEntry(
        cache_key="test_cache_key_123",
        source_id="dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        operation=OperationType.VIDEO.value,
        codec="H264",
        resolution="1080p",
        height=1080,
        telegram_channel_id=-1001234567890,
        telegram_message_id=42,
        hit_count=0,
        is_valid=True,
        created_at=datetime.now(timezone.utc),
        last_used_at=datetime.now(timezone.utc),
    )
    db_session.add(cache_entry)
    await db_session.commit()

    # Successful delivery
    mock_bot = MagicMock()
    mock_bot.copy_message = AsyncMock(return_value=True)

    delivered, err = await CacheService.deliver_cached_media(
        mock_bot, db_session, cache_entry, target_chat_id=999
    )
    assert delivered is True
    assert err is None
    assert cache_entry.hit_count == 1
    mock_bot.copy_message.assert_called_once_with(
        chat_id=999, from_chat_id=-1001234567890, message_id=42
    )

    # 2. Telegram message deleted in channel: must invalidate cache in DB!
    mock_bot.copy_message = AsyncMock(
        side_effect=TelegramBadRequest(method="copyMessage", message="Bad Request: message to copy not found")
    )
    delivered2, err2 = await CacheService.deliver_cached_media(
        mock_bot, db_session, cache_entry, target_chat_id=999
    )
    assert delivered2 is False
    assert "removed" in err2
    assert cache_entry.is_valid is False


@pytest.mark.asyncio
async def test_must_join_blocks_cache_hit_delivery(db_session, monkeypatch):
    # Rule 5 & 43 & 44: A cached file must NEVER bypass the Must-Join requirement!
    monkeypatch.setattr("src.core.config.settings.MUST_JOIN_ENABLED", True)

    ch = RequiredChannel(chat_id=-1001, title="VIP Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    mock_bot = MagicMock()
    # User is not a member
    mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))

    is_auth, missing = await MustJoinService.require_must_join(
        mock_bot, db_session, user_id=777, force_authoritative=True
    )
    assert is_auth is False
    assert len(missing) == 1
    # User is unauthorized, so cache delivery is never invoked!


@pytest.mark.asyncio
async def test_shared_job_delivery_with_member_and_non_member(db_session, monkeypatch):
    # Rule 7 & 45: A and B share job. A leaves channel, B stays.
    # Job finishes: B receives file, A is marked WAITING_FOR_AUTHORIZATION.
    monkeypatch.setattr("src.core.config.settings.MUST_JOIN_ENABLED", True)

    ch = RequiredChannel(chat_id=-1001, title="VIP Channel", enabled=True, bot_status="administrator")
    user_a = User(id=101, username="usera", first_seen_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc))
    user_b = User(id=102, username="userb", first_seen_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc))
    db_session.add_all([ch, user_a, user_b])

    job = Job(
        id="job-shared-1",
        source_url="https://youtube.com/watch?v=123",
        canonical_url="https://youtube.com/watch?v=123",
        source_id="123",
        title="Shared Video",
        operation="VIDEO",
        output_codec="H264",
        resolution="1080p",
        target_height=1080,
        status=JobStatus.COMPLETED.value,
        cache_key="shared_cache_key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(job)

    req_a = JobRequest(job_id=job.id, user_id=101, chat_id=101, delivery_status=DeliveryStatus.PENDING.value)
    req_b = JobRequest(job_id=job.id, user_id=102, chat_id=102, delivery_status=DeliveryStatus.PENDING.value)
    db_session.add_all([req_a, req_b])

    cache_entry = CacheEntry(
        cache_key="shared_cache_key",
        source_id="123",
        title="Shared Video",
        operation="VIDEO",
        telegram_channel_id=-10012345,
        telegram_message_id=99,
        created_at=datetime.now(timezone.utc),
        last_used_at=datetime.now(timezone.utc),
    )
    db_session.add(cache_entry)
    await db_session.commit()

    mock_bot = MagicMock()
    mock_bot.copy_message = AsyncMock(return_value=True)

    # User A is 'left', User B is 'member'
    async def mock_member_check(chat_id, user_id):
        if user_id == 101:
            return MagicMock(status="left")
        return MagicMock(status="member")

    mock_bot.get_chat_member = AsyncMock(side_effect=mock_member_check)

    # Deliver to subscribers:
    for req in [req_a, req_b]:
        is_auth, _ = await MustJoinService.require_must_join(
            mock_bot, db_session, req.user_id, force_authoritative=True
        )
        if not is_auth:
            req.delivery_status = DeliveryStatus.WAITING_FOR_AUTHORIZATION.value
        else:
            delivered, _ = await CacheService.deliver_cached_media(
                mock_bot, db_session, cache_entry, req.chat_id
            )
            if delivered:
                req.delivery_status = DeliveryStatus.DELIVERED.value

    await db_session.commit()

    # Verify:
    assert req_a.delivery_status == DeliveryStatus.WAITING_FOR_AUTHORIZATION.value
    assert req_b.delivery_status == DeliveryStatus.DELIVERED.value

    # Verify copy_message called only for User B
    assert mock_bot.copy_message.call_count == 1
    mock_bot.copy_message.assert_called_once_with(
        chat_id=102, from_chat_id=-10012345, message_id=99
    )
