from unittest.mock import AsyncMock, MagicMock
import pytest
from src.core.config import settings
from src.models.channel import RequiredChannel
from src.services.must_join_service import MustJoinService, is_active_member


def test_membership_state_evaluation():
    # 1. Active states
    m_creator = MagicMock(status="creator")
    m_admin = MagicMock(status="administrator")
    m_member = MagicMock(status="member")

    assert is_active_member(m_creator) is True
    assert is_active_member(m_admin) is True
    assert is_active_member(m_member) is True

    # 2. Restricted with is_member=True
    m_restricted_member = MagicMock(status="restricted", is_member=True)
    assert is_active_member(m_restricted_member) is True

    # 3. Restricted with is_member=False (e.g. muted non-member)
    m_restricted_non_member = MagicMock(status="restricted", is_member=False)
    assert is_active_member(m_restricted_non_member) is False

    # 4. Unauthorized states
    m_left = MagicMock(status="left")
    m_kicked = MagicMock(status="kicked")

    assert is_active_member(m_left) is False
    assert is_active_member(m_kicked) is False


@pytest.mark.asyncio
async def test_must_join_disabled_allows_all(db_session):
    settings.MUST_JOIN_ENABLED = False
    bot = MagicMock()

    is_auth, missing = await MustJoinService.require_must_join(bot, db_session, user_id=123)
    assert is_auth is True
    assert missing == []


@pytest.mark.asyncio
async def test_must_join_multiple_channels_logic(db_session):
    settings.MUST_JOIN_ENABLED = True

    # Add 2 channels to DB
    ch1 = RequiredChannel(chat_id=-1001, title="Channel A", enabled=True, bot_status="administrator")
    ch2 = RequiredChannel(chat_id=-1002, title="Channel B", enabled=True, bot_status="administrator")
    db_session.add_all([ch1, ch2])
    await db_session.commit()

    bot = MagicMock()

    # Case 1: User is member of ch1, but NOT ch2
    async def mock_get_chat_member(chat_id, user_id):
        if chat_id == -1001:
            return MagicMock(status="member")
        else:
            return MagicMock(status="left")

    bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member)

    is_auth, missing = await MustJoinService.require_must_join(
        bot, db_session, user_id=123, force_authoritative=True
    )
    # Must fail because user is missing Channel B
    assert is_auth is False
    assert len(missing) == 1
    assert missing[0].chat_id == -1002

    # Case 2: User joins Channel B and presses "Check Again"
    async def mock_get_chat_member_joined(chat_id, user_id):
        return MagicMock(status="member")

    bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member_joined)

    is_auth2, missing2 = await MustJoinService.require_must_join(
        bot, db_session, user_id=123, force_authoritative=True
    )
    assert is_auth2 is True
    assert len(missing2) == 0

    # Case 3: User later leaves Channel A
    async def mock_get_chat_member_left(chat_id, user_id):
        if chat_id == -1001:
            return MagicMock(status="left")
        return MagicMock(status="member")

    bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member_left)

    is_auth3, missing3 = await MustJoinService.require_must_join(
        bot, db_session, user_id=123, force_authoritative=True
    )
    # Revoked immediately!
    assert is_auth3 is False
    assert len(missing3) == 1
    assert missing3[0].chat_id == -1001
