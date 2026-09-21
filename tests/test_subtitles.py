import pytest
from src.services.ai_service import AIService
from src.services.ytdlp_service import YtDlpService


def test_english_subtitle_detection():
    # 1. Manual English exists
    info_manual = {
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {},
    }
    has_en, track, is_auto = YtDlpService.check_english_subtitles(info_manual)
    assert has_en is True
    assert track == "en"
    assert is_auto is False

    # 2. Auto-generated English exists
    info_auto = {
        "subtitles": {},
        "automatic_captions": {"en-US": [{"ext": "vtt"}]},
    }
    has_en, track, is_auto = YtDlpService.check_english_subtitles(info_auto)
    assert has_en is True
    assert track == "en-US"
    assert is_auto is True

    # 3. No English exists (only Spanish)
    info_no_en = {
        "subtitles": {"es": [{"ext": "vtt"}]},
        "automatic_captions": {"fr": [{"ext": "vtt"}]},
    }
    has_en, track, is_auto = YtDlpService.check_english_subtitles(info_no_en)
    assert has_en is False
    assert track is None

    # 4. Direct YouTube Persian exists, but NO English
    # Rule: Persian is ALWAYS AI-translated English. If English is absent, Persian must NOT be offered!
    info_direct_fa = {
        "subtitles": {"fa": [{"ext": "vtt"}]},
        "automatic_captions": {},
    }
    has_en, track, is_auto = YtDlpService.check_english_subtitles(info_direct_fa)
    assert has_en is False


def test_srt_parsing_and_validation():
    valid_srt = (
        "1\n00:00:01,000 --> 00:00:04,000\nHello and welcome!\n\n"
        "2\n00:00:04,500 --> 00:00:07,500\nThis is a production test.\n"
    )

    segments = AIService.parse_srt(valid_srt)
    assert len(segments) == 2
    assert segments[0].index == 1
    assert segments[0].start == "00:00:01,000"
    assert segments[0].end == "00:00:04,000"
    assert segments[0].text == "Hello and welcome!"

    is_valid, err = AIService.validate_srt(valid_srt)
    assert is_valid is True
    assert err is None

    # Malformed SRT (end before start)
    invalid_srt = (
        "1\n00:00:05,000 --> 00:00:02,000\nInvalid timestamp sequence!\n"
    )
    is_valid, err = AIService.validate_srt(invalid_srt)
    assert is_valid is False
    assert "End time before start time" in err


@pytest.mark.asyncio
async def test_ai_runtime_key_isolation(tmp_path, monkeypatch):
    """
    CRITICAL Regression Test:
    Worker reads AI key from /config/runtime/ai-api-key strictly without
    accessing master.key or decrypting database credentials.
    Runtime file takes precedence over stale in-memory / .env keys.
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_token_file = runtime_dir / "bot-token"
    runtime_ai_key_file = runtime_dir / "ai-api-key"

    runtime_ai_key_file.write_text("sk-runtime-authoritative-secret-key", encoding="utf-8")

    from src.core.config import settings

    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", str(runtime_token_file))
    monkeypatch.setattr(settings, "AI_API_KEY", "sk-old-stale-env-key")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-4o-mini")

    # 1. Authoritative runtime file takes precedence over .env key
    resolved_key = AIService.get_api_key()
    assert resolved_key == "sk-runtime-authoritative-secret-key"
    assert AIService.is_configured() is True

    # 2. When runtime file is absent, falls back to settings.AI_API_KEY
    runtime_ai_key_file.unlink()
    resolved_fallback = AIService.get_api_key()
    assert resolved_fallback == "sk-old-stale-env-key"

    # 3. When both are absent, returns None
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    assert AIService.get_api_key() is None
    assert AIService.is_configured() is False


@pytest.mark.asyncio
async def test_public_settings_loader_strictly_avoids_secrets(db_session, monkeypatch):
    """
    Verifies that SettingService.load_public_settings_to_runtime() never calls
    get_master_key or decrypt_credential, maintaining the strict Worker/Bot isolation boundary.
    """
    from unittest.mock import AsyncMock
    from src.services.setting_service import SettingService
    import src.services.setting_service as ss_module

    # Mock get_master_key to raise an exception if called
    mock_master_key = AsyncMock(side_effect=RuntimeError("Security violation: master.key accessed!"))
    monkeypatch.setattr(ss_module, "get_master_key", mock_master_key)

    # Calling public loader must NOT trigger master key access
    await SettingService.load_public_settings_to_runtime(db_session)
    mock_master_key.assert_not_called()


@pytest.mark.asyncio
async def test_subtitle_translation_chunking_and_retry(monkeypatch):
    """
    Verifies subtitle translation:
    - Chunks by 25 segments
    - Retries with exponential backoff on transient network failure
    - Successfully returns translated SRT preserving timestamps
    """
    from unittest.mock import AsyncMock, patch
    from src.core.config import settings

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "test-model")
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key")

    # Generate 30 segments (which must be split into 2 chunks: 25 and 5)
    segments_srt = []
    for i in range(1, 31):
        start_sec = i * 2
        end_sec = start_sec + 1
        segments_srt.append(
            f"{i}\n00:00:{start_sec:02d},000 --> 00:00:{end_sec:02d},000\nHello segment {i}\n"
        )
    raw_srt = "\n".join(segments_srt)

    attempt_counts = {"chunk1": 0, "chunk2": 0}

    class MockResponse:
        def __init__(self, text, status_code=200):
            self.status_code = status_code
            self._text = text

        def json(self):
            return {
                "choices": [
                    {"message": {"content": self._text}}
                ]
            }

        @property
        def text(self):
            return self._text

    async def mock_post(url, headers, json):
        # Inspect user prompt to see which chunk this is
        user_content = json["messages"][1]["content"]
        if "Hello segment 1\n" in user_content:
            attempt_counts["chunk1"] += 1
            if attempt_counts["chunk1"] == 1:
                # Simulate network glitch on first attempt of chunk 1
                raise Exception("Transient network connection timeout")
            # Return translated chunk 1 (25 segments)
            resp_lines = []
            for j in range(1, 26):
                s = j * 2
                e = s + 1
                resp_lines.append(f"{j}\n00:00:{s:02d},000 --> 00:00:{e:02d},000\nسلام بخش {j}\n")
            return MockResponse("\n".join(resp_lines))
        else:
            attempt_counts["chunk2"] += 1
            # Return translated chunk 2 (5 segments)
            resp_lines = []
            for j in range(26, 31):
                s = j * 2
                e = s + 1
                resp_lines.append(f"{j}\n00:00:{s:02d},000 --> 00:00:{e:02d},000\nسلام بخش {j}\n")
            return MockResponse("\n".join(resp_lines))

    # Fast sleep
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        ok, persian_srt, err = await AIService.translate_english_to_persian(raw_srt)

    assert ok is True
    assert err is None
    assert attempt_counts["chunk1"] == 2  # Retried once after failure
    assert attempt_counts["chunk2"] == 1  # Succeeded on first attempt

    parsed = AIService.parse_srt(persian_srt)
    assert len(parsed) == 30
    assert "سلام بخش 1" in parsed[0].text
    assert "سلام بخش 30" in parsed[29].text

