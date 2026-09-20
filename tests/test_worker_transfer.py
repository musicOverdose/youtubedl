import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import FSInputFile

from src.core.config import settings
from src.core.constants import OperationType
from src.models.job import Job
from src.worker.processor import JobProcessor
from src.worker.recovery import WorkerRecovery
from src.worker.telegram_factory import TelegramClientFactory


@pytest.fixture
def transfer_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    transfer_dir = os.path.join(tmp, "transfer")
    temp_dir = os.path.join(tmp, "ytdl")
    token_file = os.path.join(tmp, "runtime", "bot-token")

    os.makedirs(transfer_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(os.path.dirname(token_file), exist_ok=True)

    with open(token_file, "w") as f:
        f.write("123456:RUNTIME_TOKEN\n")

    monkeypatch.setattr(settings, "TRANSFER_DIR", transfer_dir)
    monkeypatch.setattr(settings, "TEMP_DIR", temp_dir)
    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", token_file)

    yield {
        "dir": tmp,
        "transfer": transfer_dir,
        "temp": temp_dir,
        "token": token_file,
    }
    shutil.rmtree(tmp, ignore_errors=True)


# 1. TelegramClientFactory: Token from File, Mode from Redis
@pytest.mark.asyncio
async def test_telegram_client_factory_local_mode(transfer_env):
    bot, mode = await TelegramClientFactory.get_client()
    assert bot.token == "123456:RUNTIME_TOKEN"
    assert mode == "local"
    # Local Bot API session should have is_local=True
    assert bot.session.api.is_local is True
    assert "8081" in bot.session.api.base
    await bot.session.close()


@pytest.mark.asyncio
async def test_telegram_client_factory_cloud_mode(transfer_env, monkeypatch):
    from tests.mock_redis import MockRedis
    mock_r = MockRedis()
    await mock_r.set("telegram:active:mode", "cloud")
    monkeypatch.setattr("src.worker.telegram_factory.get_redis_client", lambda: mock_r)

    bot, mode = await TelegramClientFactory.get_client()
    assert bot.token == "123456:RUNTIME_TOKEN"
    assert mode == "cloud"
    assert bot.session.api.is_local is False
    assert "api.telegram.org" in bot.session.api.base
    await bot.session.close()


# 2. Local Mode: Zero-Multipart File Handoff via URI
@pytest.mark.asyncio
async def test_local_mode_send_media_handoff(transfer_env):
    processor = JobProcessor()
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=999))

    # Create dummy video file
    dummy_video = os.path.join(transfer_env["temp"], "test_video.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"0" * 1024)

    msg_id, size = await processor._send_media_to_cache(
        bot=mock_bot,
        api_mode="local",
        channel_id=-1001234567890,
        local_file_path=dummy_video,
        operation=OperationType.VIDEO.value,
        job_id="job-123",
        job_title="My Test Video",
        caption="test caption",
    )

    assert msg_id == 999
    assert size == 1024

    # Verify send_video was called with string URI (file:///transfer/job-123/...)
    mock_bot.send_video.assert_called_once()
    call_kwargs = mock_bot.send_video.call_args.kwargs
    video_arg = call_kwargs["video"]
    assert isinstance(video_arg, str)
    assert video_arg.startswith("file://")
    assert "job-123" in video_arg
    assert video_arg.endswith(".mp4")

    # Verify transfer job dir was purged immediately post-upload
    job_transfer_dir = os.path.join(transfer_env["transfer"], "job-123")
    assert not os.path.exists(job_transfer_dir)


# 3. Cloud Mode: 50 MB Preflight Limit Check
@pytest.mark.asyncio
async def test_cloud_mode_50mb_preflight_rejection(transfer_env):
    processor = JobProcessor()
    mock_bot = MagicMock()

    # Create dummy large file (e.g. 51 MB)
    large_video = os.path.join(transfer_env["temp"], "large_video.mp4")
    with open(large_video, "wb") as f:
        # Seek to 51 MB
        f.seek(51 * 1024 * 1024)
        f.write(b"\0")

    with pytest.raises(ValueError, match="exceeds Cloud Bot API limit of 50 MB"):
        await processor._send_media_to_cache(
            bot=mock_bot,
            api_mode="cloud",
            channel_id=-1001234567890,
            local_file_path=large_video,
            operation=OperationType.VIDEO.value,
            job_id="job-large",
            job_title="Large Video",
        )

    # Bot send_video should NOT have been called
    mock_bot.send_video.assert_not_called()


@pytest.mark.asyncio
async def test_cloud_mode_under_50mb_uses_fs_input_file(transfer_env):
    processor = JobProcessor()
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=888))

    small_video = os.path.join(transfer_env["temp"], "small_video.mp4")
    with open(small_video, "wb") as f:
        f.write(b"x" * 2048)

    msg_id, size = await processor._send_media_to_cache(
        bot=mock_bot,
        api_mode="cloud",
        channel_id=-1001234567890,
        local_file_path=small_video,
        operation=OperationType.VIDEO.value,
        job_id="job-small",
        job_title="Small Video",
    )

    assert msg_id == 888
    call_kwargs = mock_bot.send_video.call_args.kwargs
    video_arg = call_kwargs["video"]
    assert isinstance(video_arg, FSInputFile)


# 4. Startup Recovery Cleans Stale Transfer Folders
@pytest.mark.asyncio
async def test_startup_recovery_cleans_transfer_dir(transfer_env, db_session):
    # Create an orphaned folder in /transfer/
    stale_folder = os.path.join(transfer_env["transfer"], "orphaned-job-999")
    os.makedirs(stale_folder, exist_ok=True)
    with open(os.path.join(stale_folder, "orphaned.mp4"), "w") as f:
        f.write("orphan")

    assert os.path.exists(stale_folder)

    # Run startup recovery
    await WorkerRecovery.perform_startup_recovery(db_session)

    # Verify orphaned folder was removed
    assert not os.path.exists(stale_folder)
