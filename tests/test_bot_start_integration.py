import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramUnauthorizedError

from src.bot.bot_instance import get_bot, reset_bot
from src.bot.handlers.base import cmd_start
from src.bot.main import check_bot_connectivity
from src.core.config import settings
from src.services.setting_service import (
    DEFAULT_WELCOME_MESSAGE,
    SettingService,
    SETTING_WELCOME_MSG,
)


@pytest.fixture(autouse=True)
def setup_runtime_bot_token(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    token_file = Path(temp_dir) / "bot-token"
    token_file.write_text("123456789:ABCdefGHIjklMNOpqrSTUvwxYZ", encoding="utf-8")
    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", str(token_file))
    reset_bot()
    yield
    reset_bot()


@pytest.mark.asyncio
async def test_cmd_start_resilience_when_user_persistence_fails():
    """
    BUG 3 Regression: If user persistence or DB access fails during /start,
    the bot MUST NOT crash; it must log a warning and still reply to the user.
    """
    mock_user = MagicMock()
    mock_user.id = 987654321
    mock_user.username = "resilient_user"
    mock_user.first_name = "Resilient"

    mock_msg = MagicMock()
    mock_msg.from_user = mock_user
    mock_msg.answer = AsyncMock()

    # Force get_or_create_user to raise an exception
    with patch("src.bot.handlers.base.get_or_create_user", side_effect=RuntimeError("Database connection lost")):
        await cmd_start(mock_msg)

    # Bot must still have answered the user
    mock_msg.answer.assert_called_once()
    answer_text = mock_msg.answer.call_args[0][0]
    assert "Resilient" in answer_text
    assert mock_msg.answer.call_args[1].get("parse_mode") == ParseMode.HTML


@pytest.mark.asyncio
async def test_cmd_start_resilience_when_welcome_message_db_fails():
    """
    BUG 3 Regression: If SettingService.get_welcome_message() fails due to DB error,
    it must fall back to DEFAULT_WELCOME_MESSAGE and answer successfully.
    """
    mock_user = MagicMock()
    mock_user.id = 987654322
    mock_user.username = "db_fail_user"
    mock_user.first_name = "DBFailUser"

    mock_msg = MagicMock()
    mock_msg.from_user = mock_user
    mock_msg.answer = AsyncMock()

    with patch("src.services.setting_service.SettingService.get_welcome_message", side_effect=RuntimeError("DB query failed")):
        await cmd_start(mock_msg)

    mock_msg.answer.assert_called_once()
    answer_text = mock_msg.answer.call_args[0][0]
    assert "DBFailUser" in answer_text
    assert "Features:" in answer_text


def test_derive_endpoint_local_vs_cloud():
    """
    BUG 3 Root cause: The bot must not use settings.TELEGRAM_API_BASE_URL directly
    with is_local=True because settings.TELEGRAM_API_BASE_URL is 'https://api.telegram.org' by default.
    SettingService.derive_endpoint must return the correct endpoint based on mode.
    """
    local_ep = SettingService.derive_endpoint("local")
    assert local_ep == "http://telegram-bot-api:8081"

    cloud_ep = SettingService.derive_endpoint("cloud")
    assert cloud_ep == "https://api.telegram.org"


@pytest.mark.asyncio
async def test_check_bot_connectivity_success():
    bot = get_bot()
    mock_me = MagicMock()
    mock_me.id = 123456789
    mock_me.username = "test_ytdl_bot"
    mock_me.first_name = "Test YTDL Bot"

    with patch.object(bot, "get_me", AsyncMock(return_value=mock_me)):
        me = await check_bot_connectivity(bot, "local", "http://telegram-bot-api:8081")
        assert me.id == 123456789
        assert me.username == "test_ytdl_bot"


@pytest.mark.asyncio
async def test_check_bot_connectivity_unauthorized():
    bot = get_bot()
    with patch.object(bot, "get_me", AsyncMock(side_effect=TelegramUnauthorizedError(method="getMe", message="Unauthorized"))):
        with pytest.raises(TelegramUnauthorizedError):
            await check_bot_connectivity(bot, "local", "http://telegram-bot-api:8081")
