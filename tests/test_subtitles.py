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
    monkeypatch.setattr(settings, "AI_CHUNK_SIZE", 25)

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


@pytest.mark.asyncio
async def test_ai_translation_max_chunks_limit(monkeypatch):
    """
    Verifies that when subtitle chunks exceed AI_MAX_CHUNKS,
    AIService.translate_english_to_persian immediately rejects it with a clear,
    helpful message advising the user to download English subtitles.
    """
    from src.core.config import settings

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "test-model")
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "AI_CHUNK_SIZE", 25)
    monkeypatch.setattr(settings, "AI_MAX_CHUNKS", 2)  # Max 2 chunks allowed

    # Generate 60 segments (which requires 3 chunks of 25: 25, 25, 10 > max 2)
    segments_srt = []
    for i in range(1, 61):
        m1, s1 = divmod(i * 2, 60)
        m2, s2 = divmod(i * 2 + 1, 60)
        segments_srt.append(f"{i}\n00:{m1:02d}:{s1:02d},000 --> 00:{m2:02d}:{s2:02d},000\nSegment {i}\n")
    raw_srt = "\n".join(segments_srt)

    ok, result, err = await AIService.translate_english_to_persian(raw_srt)
    assert ok is False
    assert result is None
    assert "Video subtitle size is too large for AI translation" in err
    assert "3 chunks / 60 cues exceeds maximum allowed limit of 2 chunks / 50 cues" in err
    assert "Please download English subtitles instead." in err


@pytest.mark.asyncio
async def test_ai_translation_detailed_error_reporting(monkeypatch):
    """
    Verifies that when the AI provider returns an error (e.g., HTTP 401 Unauthorized or 429 Quota),
    the returned error message includes the chunk index, HTTP status code, and error body snippet.
    """
    from unittest.mock import AsyncMock, patch
    from src.core.config import settings

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "AI_API_KEY", "sk-invalid-test-key")
    monkeypatch.setattr(settings, "AI_MAX_CHUNKS", 10)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    class MockErrorResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self._text = text

        @property
        def text(self):
            return self._text

        def json(self):
            return {"error": {"message": self._text}}

    sample_srt = (
        "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"
    )

    # 1. Test HTTP 401 Unauthorized
    async def mock_401(url, headers, json):
        return MockErrorResponse(401, "Incorrect API key provided: sk-invalid...")

    with patch("httpx.AsyncClient.post", side_effect=mock_401):
        ok, res, err = await AIService.translate_english_to_persian(sample_srt)
        assert ok is False
        assert res is None
        assert "chunk 1/1" in err
        assert "HTTP 401" in err
        assert "Incorrect API key provided" in err

    # 2. Test HTTP 429 Rate Limit / Quota Exceeded
    async def mock_429(url, headers, json):
        return MockErrorResponse(429, "You exceeded your current quota, please check your plan")

    with patch("httpx.AsyncClient.post", side_effect=mock_429):
        ok, res, err = await AIService.translate_english_to_persian(sample_srt)
        assert ok is False
        assert res is None
        assert "chunk 1/1" in err
        assert "HTTP 429" in err
        assert "You exceeded your current quota" in err


@pytest.mark.asyncio
async def test_ai_base_url_normalization(monkeypatch):
    """
    Verifies that base URLs with or without trailing slashes and with or without
    /chat/completions are properly formatted without duplicating /chat/completions.
    """
    from unittest.mock import patch
    from src.core.config import settings

    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "AI_API_KEY", "sk-test")

    recorded_urls = []

    class MockOKResponse:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "1\n00:00:01,000 --> 00:00:03,000\nسلام\n"}}]}

    async def mock_post(url, headers, json):
        recorded_urls.append(url)
        return MockOKResponse()

    sample_srt = "1\n00:00:01,000 --> 00:00:03,000\nHello\n"

    # Test URL ending with /chat/completions
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1/chat/completions/")
    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        await AIService.translate_english_to_persian(sample_srt)
    assert recorded_urls[-1] == "https://api.openai.com/v1/chat/completions"

    # Test standard base URL
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1/")
    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        await AIService.translate_english_to_persian(sample_srt)
    assert recorded_urls[-1] == "https://api.openai.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_status_notifier_html_escaping():
    """
    Verifies that StatusNotifier escapes HTML special characters in detail messages
    to prevent parse failures with Telegram HTML mode.
    """
    from unittest.mock import AsyncMock
    from src.worker.notifier import StatusNotifier

    mock_bot = AsyncMock()
    requests = [{"chat_id": 12345, "message_id": 67890}]
    notifier = StatusNotifier(mock_bot, requests)

    # Message with HTML tags and entities
    await notifier.update("❌", "Failed", "Error: <Response [401]> & 'test' key", force=True)

    mock_bot.edit_message_text.assert_called_once()
    called_kwargs = mock_bot.edit_message_text.call_args[1]
    assert called_kwargs["chat_id"] == 12345
    assert called_kwargs["message_id"] == 67890
    # Check that <Response [401]> is escaped as &lt;Response [401]&gt;
    assert "&lt;Response [401]&gt;" in called_kwargs["text"]
    assert "&amp;" in called_kwargs["text"]


@pytest.mark.asyncio
async def test_worker_progress_hook_event_loop_safety():
    """
    CRITICAL Regression Test:
    Verifies that calling progress_hook from inside a secondary thread (such as
    run_in_executor named asyncio_0) schedules the update cleanly without raising:
    RuntimeError: There is no current event loop in thread 'asyncio_0'.
    """
    import asyncio
    from unittest.mock import AsyncMock
    from src.worker.notifier import StatusNotifier

    mock_bot = AsyncMock()
    requests = [{"chat_id": 111, "message_id": 222}]
    notifier = StatusNotifier(mock_bot, requests)

    loop = asyncio.get_running_loop()

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total) * 100
            asyncio.run_coroutine_threadsafe(
                notifier.update("⬇️", "Downloading", f"Progress: {pct:.1f}%", force=True),
                loop,
            )

    def worker_thread():
        # Simulate secondary thread execution by yt-dlp
        progress_hook({
            "status": "downloading",
            "total_bytes": 1000,
            "downloaded_bytes": 500,
        })

    # Run inside executor thread
    await loop.run_in_executor(None, worker_thread)
    # Allow event loop to process scheduled coroutine
    await asyncio.sleep(0.05)

    assert mock_bot.edit_message_text.called
    called_text = mock_bot.edit_message_text.call_args[1]["text"]
    assert "Progress: 50.0%" in called_text


