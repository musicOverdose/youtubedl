import os
import tempfile
from pathlib import Path
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.core.config import settings
from src.models.setting import Setting
from src.core.security import create_session_token
from src.services.setting_service import (
    SettingService,
    SETTING_MAX_VIDEO_DURATION_SECONDS,
    SETTING_MAX_CONCURRENT_PER_USER,
    SETTING_MAX_QUEUED_PER_USER,
    SETTING_MAX_TEMP_STORAGE_GB,
    SETTING_DEFAULT_MAX_HEIGHT,
    SETTING_ALLOW_UNKNOWN_DURATION,
    SETTING_CACHE_HIT_BYPASSES_DURATION_LIMIT,
    SETTING_PLAYLISTS_ENABLED,
    SETTING_H264_ENABLED,
    SETTING_H265_ENABLED,
    SETTING_MP3_ENABLED,
    SETTING_SUBTITLES_ENABLED,
    SETTING_AI_ENABLED,
    SETTING_AI_PROVIDER,
    SETTING_AI_BASE_URL,
    SETTING_AI_MODEL,
    SETTING_AI_API_KEY,
    SETTING_YTDLP_COOKIES_ENABLED,
    SETTING_MUST_JOIN_ENABLED,
    SETTING_MAX_ACTIVE_JOBS,
)
from src.web.app import app


@pytest.fixture(autouse=True)
def setup_temp_runtime_and_master_dir(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    master_dir = Path(temp_dir) / "master"
    runtime_dir = Path(temp_dir) / "runtime"
    master_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    master_key_file = master_dir / "master.key"
    runtime_token_file = runtime_dir / "bot-token"
    runtime_ai_key_file = runtime_dir / "ai-api-key"

    monkeypatch.setattr(settings, "MASTER_KEY_FILE", str(master_key_file))
    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", str(runtime_token_file))

    # Initialize master key
    master_key_file.write_text(Fernet.generate_key().decode("utf-8"), encoding="utf-8")
    yield
    # Cleanup


@pytest.mark.asyncio
async def test_save_and_get_application_settings_persistence(db_session):
    # 1. Update settings via SettingService
    test_payload = {
        "max_video_duration_seconds": 7200,
        "allow_unknown_duration": True,
        "cache_hit_bypasses_duration_limit": False,
        "max_concurrent_per_user": 5,
        "max_queued_per_user": 10,
        "max_temp_storage_gb": 40.0,
        "default_max_height": 1080,
        "playlists_enabled": False,
        "h264_enabled": True,
        "h265_enabled": True,
        "mp3_enabled": True,
        "subtitles_enabled": False,
    }

    saved = await SettingService.save_application_settings(test_payload)
    for k, v in test_payload.items():
        assert saved[k] == v

    # Verify settings object in memory updated
    assert settings.MAX_VIDEO_DURATION_SECONDS == 7200
    assert settings.ALLOW_UNKNOWN_DURATION is True
    assert settings.CACHE_HIT_BYPASSES_DURATION_LIMIT is False
    assert settings.MAX_CONCURRENT_PER_USER == 5
    assert settings.MAX_QUEUED_PER_USER == 10
    assert settings.MAX_TEMP_STORAGE_GB == 40.0
    assert settings.DEFAULT_MAX_HEIGHT == 1080
    assert settings.PLAYLISTS_ENABLED is False
    assert settings.SUBTITLES_ENABLED is False

    # 2. Verify rows in PostgreSQL
    res = await db_session.execute(
        select(Setting).where(Setting.key == SETTING_MAX_VIDEO_DURATION_SECONDS, Setting.status == "ACTIVE")
    )
    row = res.scalar_one_or_none()
    assert row is not None
    assert row.value == "7200"

    # 3. Simulate process restart: mutate in-memory settings back to defaults
    settings.MAX_VIDEO_DURATION_SECONDS = 1800
    settings.ALLOW_UNKNOWN_DURATION = False
    settings.MAX_CONCURRENT_PER_USER = 1
    settings.DEFAULT_MAX_HEIGHT = 720

    # Call load_all_settings_to_runtime() as done on startup
    await SettingService.load_all_settings_to_runtime()

    # Verify restored from PostgreSQL
    assert settings.MAX_VIDEO_DURATION_SECONDS == 7200
    assert settings.ALLOW_UNKNOWN_DURATION is True
    assert settings.MAX_CONCURRENT_PER_USER == 5
    assert settings.DEFAULT_MAX_HEIGHT == 1080


@pytest.mark.asyncio
async def test_settings_api_endpoints_persistence(db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Require authentication
        res = await client.get("/api/settings")
        assert res.status_code == 401

        admin_token = create_session_token("admin", role="ADMIN")
        client.cookies.set("session_token", admin_token)
        client.headers.update({"Authorization": f"Bearer {admin_token}"})

        # Save new settings via POST
        post_data = {
            "max_video_duration_seconds": 5400,
            "allow_unknown_duration": True,
            "cache_hit_bypasses_duration_limit": True,
            "max_concurrent_per_user": 3,
            "max_queued_per_user": 7,
            "max_temp_storage_gb": 35,
            "default_max_height": 1080,
            "playlists_enabled": True,
            "h264_enabled": True,
            "h265_enabled": False,
            "mp3_enabled": True,
            "subtitles_enabled": True,
        }
        res_post = await client.post("/api/settings", json=post_data)
        assert res_post.status_code == 200
        assert res_post.json()["status"] == "updated"

        # GET to verify read from DB
        res_get = await client.get("/api/settings")
        assert res_get.status_code == 200
        get_data = res_get.json()
        assert get_data["max_video_duration_seconds"] == 5400
        assert get_data["h265_enabled"] is False
        assert get_data["max_queued_per_user"] == 7


@pytest.mark.asyncio
async def test_ai_settings_persistence_and_encryption(db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_token = create_session_token("admin", role="ADMIN")
        client.cookies.set("session_token", admin_token)
        client.headers.update({"Authorization": f"Bearer {admin_token}"})

        # Save AI settings with a secret API key (AISettingsRequest schema)
        ai_payload = {
            "enabled": True,
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-proj-supersecretkey1234567890",
        }
        res_post = await client.post("/api/ai", json=ai_payload)
        assert res_post.status_code == 200
        assert res_post.json()["status"] == "updated"

        # Verify via GET /api/ai
        res_get = await client.get("/api/ai")
        assert res_get.status_code == 200
        data = res_get.json()
        assert data["enabled"] is True
        assert data["provider"] == "openai"
        assert data["model"] == "gpt-4o-mini"
        assert data["api_key_masked"] != ""
        assert "supersecretkey" not in data["api_key_masked"]  # Must be masked

        # Check DB row is encrypted
        res = await db_session.execute(
            select(Setting).where(Setting.key == SETTING_AI_API_KEY, Setting.status == "ACTIVE")
        )
        row = res.scalar_one_or_none()
        assert row is not None
        assert row.is_encrypted is True
        assert "supersecretkey" not in row.value  # Encrypted ciphertext

        # Check runtime secret file created
        runtime_key_file = Path(settings.RUNTIME_BOT_TOKEN_FILE).parent / "ai-api-key"
        assert runtime_key_file.exists()
        assert runtime_key_file.read_text(encoding="utf-8").strip() == "sk-proj-supersecretkey1234567890"

        # Simulate restart: reset in-memory settings
        settings.AI_ENABLED = False
        settings.AI_MODEL = "default-model"
        settings.AI_API_KEY = ""

        await SettingService.load_all_settings_to_runtime()
        assert settings.AI_ENABLED is True
        assert settings.AI_MODEL == "gpt-4o-mini"
        assert settings.AI_API_KEY == "sk-proj-supersecretkey1234567890"


@pytest.mark.asyncio
async def test_audited_single_settings_persistence(db_session):
    # 1. Cookies toggle
    await SettingService.save_single_setting(SETTING_YTDLP_COOKIES_ENABLED, True)
    assert settings.YTDLP_COOKIES_ENABLED is True
    res = await db_session.execute(
        select(Setting).where(Setting.key == SETTING_YTDLP_COOKIES_ENABLED, Setting.status == "ACTIVE")
    )
    assert res.scalar_one_or_none().value == "true"

    # 2. Must-Join toggle
    await SettingService.save_single_setting(SETTING_MUST_JOIN_ENABLED, False)
    assert settings.MUST_JOIN_ENABLED is False
    res = await db_session.execute(
        select(Setting).where(Setting.key == SETTING_MUST_JOIN_ENABLED, Setting.status == "ACTIVE")
    )
    assert res.scalar_one_or_none().value == "false"

    # 3. Max active jobs concurrency
    await SettingService.save_single_setting(SETTING_MAX_ACTIVE_JOBS, 7)
    assert settings.MAX_ACTIVE_JOBS == 7
    res = await db_session.execute(
        select(Setting).where(Setting.key == SETTING_MAX_ACTIVE_JOBS, Setting.status == "ACTIVE")
    )
    assert res.scalar_one_or_none().value == "7"
