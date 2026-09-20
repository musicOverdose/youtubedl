import html
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from src.bot.bot_instance import get_bot, reset_bot
from src.bot.handlers.base import cmd_start
from src.core.config import settings
from src.core.security import create_session_token
from src.services.setting_service import (
    DEFAULT_WELCOME_MESSAGE,
    SettingService,
    validate_telegram_html,
)
from src.web.app import app


# ==============================================================================
# 1. HTML Validation Tests
# ==============================================================================

def test_telegram_html_validator_valid():
    valid_samples = [
        "👋 Hello, <b>{first_name}</b>!",
        "Check this <i>italic text</i> and <u>underline</u> and <s>strike</s>.",
        "Here is <tg-spoiler>a secret</tg-spoiler>!",
        'Visit <a href="https://t.me/telegram">Telegram</a> for details.',
        '<pre><code class="language-python">print("Hello World")</code></pre>',
        '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>',
        "Plain text without any tags is completely fine.",
    ]
    for sample in valid_samples:
        is_valid, err = validate_telegram_html(sample)
        assert is_valid is True, f"Expected valid for '{sample}', but got: {err}"
        assert err == ""


def test_telegram_html_validator_invalid_tags_and_attrs():
    invalid_samples = [
        ("<script>alert(1)</script>", "not supported"),
        ('<b style="color:red">bold</b>', "does not allow attributes"),
        ('<a target="_blank" href="https://foo.com">link</a>', "does not support attribute"),
        ('<tg-emoji>no id</tg-emoji>', "requires a non-empty 'emoji-id' attribute"),
        ("<div>content</div>", "not supported"),
        ("", "cannot be empty"),
        ("   ", "cannot be empty"),
    ]
    for sample, expected_err_part in invalid_samples:
        is_valid, err = validate_telegram_html(sample)
        assert is_valid is False, f"Expected invalid for '{sample}'"
        assert expected_err_part.lower() in err.lower()


def test_telegram_html_validator_mismatched_and_unclosed_tags():
    mismatched = [
        "<b><i>text</b></i>",
        "<b>unclosed tag",
        "</i>unmatched close",
    ]
    for sample in mismatched:
        is_valid, err = validate_telegram_html(sample)
        assert is_valid is False, f"Expected invalid for mismatched '{sample}'"
        assert len(err) > 0


# ==============================================================================
# 2. SettingService Welcome Message Persistence Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_save_and_reset_welcome_message():
    # 1. Initially default template
    msg = await SettingService.get_welcome_message()
    assert msg == DEFAULT_WELCOME_MESSAGE

    # 2. Save valid custom template
    custom = "👋 Welcome <b>{first_name}</b> to our bot!\nEnjoy downloads."
    saved = await SettingService.save_welcome_message(custom)
    assert saved == custom

    # Verify retrieval
    active = await SettingService.get_welcome_message()
    assert active == custom

    # 3. Attempt to save invalid HTML -> HTTPException(400)
    with pytest.raises(HTTPException) as exc:
        await SettingService.save_welcome_message("Hello <b>{first_name}<i>bad nesting</b></i>")
    assert exc.value.status_code == 400
    assert "Invalid Telegram HTML" in exc.value.detail

    # 4. Reset welcome message -> restored to DEFAULT_WELCOME_MESSAGE
    reset_val = await SettingService.reset_welcome_message()
    assert reset_val == DEFAULT_WELCOME_MESSAGE

    # Verify retrieval after reset
    active_after_reset = await SettingService.get_welcome_message()
    assert active_after_reset == DEFAULT_WELCOME_MESSAGE


# ==============================================================================
# 3. Web API Endpoints Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_welcome_message_endpoints_auth_and_crud():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated requests must be rejected with 401
        res_unauth_get = await client.get("/api/telegram/welcome-message")
        assert res_unauth_get.status_code == 401

        res_unauth_post = await client.post("/api/telegram/welcome-message", json={"message": "hi"})
        assert res_unauth_post.status_code == 401

        res_unauth_reset = await client.post("/api/telegram/welcome-message/reset")
        assert res_unauth_reset.status_code == 401

        # 2. Authenticated requests
        admin_token = create_session_token("admin", role="ADMIN")
        client.cookies.set("session_token", admin_token)
        client.headers.update({"Authorization": f"Bearer {admin_token}"})

        # GET active message
        res_get = await client.get("/api/telegram/welcome-message")
        assert res_get.status_code == 200
        assert "message" in res_get.json()

        # POST valid message
        new_msg = "Hello <b>{first_name}</b>!\nDownload videos here."
        res_post = await client.post("/api/telegram/welcome-message", json={"message": new_msg})
        assert res_post.status_code == 200
        assert res_post.json()["status"] == "saved"
        assert res_post.json()["message"] == new_msg

        # POST invalid message -> 400
        res_post_bad = await client.post("/api/telegram/welcome-message", json={"message": "<script>bad</script>"})
        assert res_post_bad.status_code == 400

        # POST reset
        res_reset = await client.post("/api/telegram/welcome-message/reset")
        assert res_reset.status_code == 200
        assert res_reset.json()["status"] == "reset"
        assert res_reset.json()["message"] == DEFAULT_WELCOME_MESSAGE


# ==============================================================================
# 4. Bot Handler Formatting & Error Fallback Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_cmd_start_formatting_and_escaping():
    mock_user = MagicMock()
    mock_user.id = 12345
    mock_user.username = "testuser"
    mock_user.first_name = "<b>Hacker & Friend</b>"

    mock_msg = MagicMock()
    mock_msg.from_user = mock_user
    mock_msg.answer = AsyncMock()

    # Save a template with {first_name}
    await SettingService.save_welcome_message("Welcome, {first_name}!")

    await cmd_start(mock_msg)

    mock_msg.answer.assert_called_once()
    called_text = mock_msg.answer.call_args[0][0]
    # Verify the first_name was properly escaped
    assert "&lt;b&gt;Hacker &amp; Friend&lt;/b&gt;" in called_text
    assert "<b>Hacker" not in called_text


@pytest.mark.asyncio
async def test_cmd_start_fallback_on_telegram_entity_error():
    mock_user = MagicMock()
    mock_user.id = 99999
    mock_user.username = "alice"
    mock_user.first_name = "Alice"

    mock_msg = MagicMock()
    mock_msg.from_user = mock_user

    # First call to answer raises TelegramBadRequest with entity parsing issue
    # Second call (fallback) succeeds
    bad_request_err = TelegramBadRequest(
        method="sendMessage",
        message="Bad Request: can't parse entities in message text: unexpected end of tag",
    )
    mock_msg.answer = AsyncMock(side_effect=[bad_request_err, None])

    # Save custom template
    await SettingService.save_welcome_message("Custom template {first_name}")

    await cmd_start(mock_msg)

    # answer should have been called twice (once with custom, once with fallback)
    assert mock_msg.answer.call_count == 2
    fallback_call_text = mock_msg.answer.call_args_list[1][0][0]
    assert "Features:" in fallback_call_text
    assert "Alice" in fallback_call_text


# ==============================================================================
# 5. Strict Bot Token Isolation Tests
# ==============================================================================

def test_bot_instance_reads_strictly_from_runtime_file(monkeypatch):
    reset_bot()
    tmp_dir = tempfile.mkdtemp()
    token_file = Path(tmp_dir) / "bot-token"

    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "BOT_TOKEN", "SHOULD_NEVER_BE_USED")

    # 1. If file does not exist -> RuntimeError
    with pytest.raises(RuntimeError) as exc1:
        get_bot()
    assert "does not exist" in str(exc1.value)

    # 2. If file is empty -> RuntimeError
    token_file.write_text("   \n", encoding="utf-8")
    reset_bot()
    with pytest.raises(RuntimeError) as exc2:
        get_bot()
    assert "is empty" in str(exc2.value)

    # 3. Valid token in file -> initialized successfully with that token
    valid_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
    token_file.write_text(valid_token, encoding="utf-8")
    reset_bot()
    bot = get_bot()
    assert bot is not None
    assert bot.token == valid_token
    reset_bot()
