import glob
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.core.config import settings
from src.core.constants import (
    REDIS_KEY_SUBTITLE_CACHE_PREFIX,
    REDIS_KEY_SUBTITLE_FA_CACHE_PREFIX,
    JobStatus,
    OperationType,
)
from src.models.job import Job
from src.services.ai_service import AIService
from src.services.setting_service import SettingService
from src.services.ytdlp_service import YtDlpService
from src.worker.processor import JobProcessor


def test_detect_manual_persian_subtitles():
    """Manual Persian subtitle track is detected across supported language codes."""
    for code in ["fa", "fa-IR", "per", "fas"]:
        info = {
            "subtitles": {code: [{"ext": "vtt", "url": "http://example.com/fa.vtt"}]},
            "automatic_captions": {},
        }
        has_fa, track_key, is_auto = YtDlpService.find_persian_subtitles(info)
        assert has_fa is True
        assert track_key == code
        assert is_auto is False


def test_detect_automatic_persian_captions():
    """Automatic Persian caption is detected when manual subtitles are absent."""
    info = {
        "subtitles": {},
        "automatic_captions": {
            "fa": [{"ext": "vtt", "url": "http://example.com/fa_auto.vtt"}]
        },
    }
    has_fa, track_key, is_auto = YtDlpService.find_persian_subtitles(info)
    assert has_fa is True
    assert track_key == "fa"
    assert is_auto is True


def test_manual_preferred_over_automatic():
    """Manual Persian track takes strict precedence over automatic captions."""
    info = {
        "subtitles": {
            "fa": [{"ext": "vtt", "url": "http://example.com/manual.vtt"}]
        },
        "automatic_captions": {
            "fa": [{"ext": "vtt", "url": "http://example.com/auto.vtt"}]
        },
    }
    has_fa, track_key, is_auto = YtDlpService.find_persian_subtitles(info)
    assert has_fa is True
    assert track_key == "fa"
    assert is_auto is False  # Must be manual, NOT auto


def test_no_persian_detected():
    """When only other languages exist, Persian is reported absent."""
    info = {
        "subtitles": {"en": [{"ext": "vtt"}], "es": [{"ext": "vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt"}]},
    }
    has_fa, track_key, is_auto = YtDlpService.find_persian_subtitles(info)
    assert has_fa is False
    assert track_key is None
    assert is_auto is False


def test_vtt_to_srt_normalization_without_ai():
    """VTT with styling and tags is normalized locally into standard SRT without AI."""
    sample_vtt = """WEBVTT
Kind: captions
Language: fa

00:00:01.000 --> 00:00:04.000 align:start position:0%
<c>سلام</c> <00:00:02.000><c>و</c> خوش آمدید

00:00:04.500 --> 00:00:08.200 line:0
<font color="white">این یک تست زیرنویس است</font>
"""
    segs = AIService.parse_srt(sample_vtt)
    assert len(segs) == 2
    assert segs[0].start == "00:00:01,000"
    assert segs[0].end == "00:00:04,000"
    assert segs[0].text == "سلام و خوش آمدید"
    assert segs[1].start == "00:00:04,500"
    assert segs[1].end == "00:00:08,200"
    assert segs[1].text == "این یک تست زیرنویس است"

    # Convert to standard SRT
    standard_srt = "\n".join(seg.to_srt() for seg in segs)
    assert "00:00:01,000 --> 00:00:04,000" in standard_srt
    assert "<c>" not in standard_srt
    assert "<font" not in standard_srt


@pytest.mark.asyncio
async def test_persian_cache_hit_bypasses_youtube_and_ai(tmp_path):
    """If Redis has cached Persian subtitles, worker uploads directly without YouTube or AI."""
    processor = JobProcessor()
    mock_bot = AsyncMock()
    processor.bot = mock_bot

    job = Job(
        id="job-fa-cache",
        source_url="https://www.youtube.com/watch?v=cached_vid",
        canonical_url="https://www.youtube.com/watch?v=cached_vid",
        source_id="cached_vid",
        title="Persian Cached Video",
        operation=OperationType.SUBTITLE.value,
        subtitle_lang="FA",
        status=JobStatus.QUEUED.value,
    )

    cached_persian_srt = "1\n00:00:01,000 --> 00:00:03,000\nسلام دنیا\n"

    with patch("src.worker.processor.get_redis_client") as mock_redis_getter, \
         patch.object(SettingService, "load_public_settings_to_runtime", AsyncMock(return_value={"telegram_cache_channel_id": "-1001234567890", "telegram_api_mode": "local"})), \
         patch.object(YtDlpService, "download_subtitles", AsyncMock()) as mock_dl_subs, \
         patch.object(AIService, "translate_english_to_persian", AsyncMock()) as mock_ai_trans, \
         patch.object(processor, "_send_media_to_cache", AsyncMock(return_value=(123, 500))), \
         patch("src.worker.processor.AsyncSessionLocal") as mock_session_cls, \
         patch("src.worker.processor.StatusNotifier") as mock_notifier_cls:

        mock_redis = AsyncMock()
        mock_redis.get.side_effect = lambda k: cached_persian_srt if "subs:fa:" in k else None
        mock_redis_getter.return_value = mock_redis

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_result.scalars.return_value.first.return_value = job
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await processor.process_job("job-fa-cache")

        # YouTube subtitle download and AI translation must NEVER be called
        mock_dl_subs.assert_not_called()
        mock_ai_trans.assert_not_called()
        assert job.status == JobStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_direct_persian_path_downloads_and_bypasses_ai(tmp_path):
    """When native Persian subtitles exist, worker downloads Persian track and NEVER calls AI."""
    processor = JobProcessor()
    mock_bot = AsyncMock()
    processor.bot = mock_bot

    job = Job(
        id="job-fa-direct",
        source_url="https://www.youtube.com/watch?v=direct_fa_vid",
        canonical_url="https://www.youtube.com/watch?v=direct_fa_vid",
        source_id="direct_fa_vid",
        title="Native Persian Video",
        operation=OperationType.SUBTITLE.value,
        subtitle_lang="FA",
        status=JobStatus.QUEUED.value,
    )

    info_with_persian = {
        "id": "direct_fa_vid",
        "subtitles": {"fa": [{"ext": "vtt"}]},
        "automatic_captions": {},
    }

    raw_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nدرود بر شما\n"

    async def fake_download_subtitles(url, output_template, cookies_file=None, on_retry=None, langs=None):
        # Simulate writing Persian vtt file to job directory
        out_file = output_template.replace("%(ext)s", "fa.vtt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(raw_vtt)

    with patch("src.worker.processor.get_redis_client") as mock_redis_getter, \
         patch.object(SettingService, "load_public_settings_to_runtime", AsyncMock(return_value={"telegram_cache_channel_id": "-1001234567890", "telegram_api_mode": "local"})), \
         patch.object(YtDlpService, "extract_metadata", AsyncMock(return_value=info_with_persian)), \
         patch.object(YtDlpService, "download_subtitles", AsyncMock(side_effect=fake_download_subtitles)) as mock_dl_subs, \
         patch.object(AIService, "translate_english_to_persian", AsyncMock()) as mock_ai_trans, \
         patch.object(processor, "_send_media_to_cache", AsyncMock(return_value=(123, 500))), \
         patch("src.worker.processor.AsyncSessionLocal") as mock_session_cls, \
         patch("src.worker.processor.StatusNotifier") as mock_notifier_cls:

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # Cache miss
        mock_redis_getter.return_value = mock_redis

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_result.scalars.return_value.first.return_value = job
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await processor.process_job("job-fa-direct")

        # Must have downloaded Persian track directly
        mock_dl_subs.assert_called_once()
        assert "fa" in mock_dl_subs.call_args.kwargs.get("langs", [])

        # AI translation MUST NEVER be called
        mock_ai_trans.assert_not_called()
        assert job.status == JobStatus.COMPLETED.value

        # Persian subtitle must be cached in Redis with fa prefix
        matching_calls = [c for c in mock_redis.set.call_args_list if "subs:fa:direct_fa_vid" in str(c)]
        assert len(matching_calls) == 1
        assert "ytdl:subs:fa:direct_fa_vid" in matching_calls[0][0][0]


@pytest.mark.asyncio
async def test_persian_fallback_to_english_and_ai_when_no_native(tmp_path):
    """When no Persian subtitles exist on YouTube, worker falls back to English + AI translation."""
    processor = JobProcessor()
    mock_bot = AsyncMock()
    processor.bot = mock_bot

    job = Job(
        id="job-fa-ai-fallback",
        source_url="https://www.youtube.com/watch?v=no_fa_vid",
        canonical_url="https://www.youtube.com/watch?v=no_fa_vid",
        source_id="no_fa_vid",
        title="English Only Video",
        operation=OperationType.SUBTITLE.value,
        subtitle_lang="FA",
        status=JobStatus.QUEUED.value,
    )

    info_without_persian = {
        "id": "no_fa_vid",
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {},
    }

    raw_en_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello World\n"
    translated_fa_srt = "1\n00:00:01,000 --> 00:00:03,000\nسلام دنیا\n"

    async def fake_download_subtitles(url, output_template, cookies_file=None, on_retry=None, langs=None):
        out_file = output_template.replace("%(ext)s", "en.vtt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(raw_en_vtt)

    with patch("src.worker.processor.get_redis_client") as mock_redis_getter, \
         patch.object(SettingService, "load_public_settings_to_runtime", AsyncMock(return_value={"telegram_cache_channel_id": "-1001234567890", "telegram_api_mode": "local"})), \
         patch.object(YtDlpService, "extract_metadata", AsyncMock(return_value=info_without_persian)), \
         patch.object(YtDlpService, "download_subtitles", AsyncMock(side_effect=fake_download_subtitles)) as mock_dl_subs, \
         patch.object(AIService, "translate_english_to_persian", AsyncMock(return_value=(True, translated_fa_srt, None))) as mock_ai_trans, \
         patch.object(processor, "_send_media_to_cache", AsyncMock(return_value=(123, 500))), \
         patch("src.worker.processor.AsyncSessionLocal") as mock_session_cls, \
         patch("src.worker.processor.StatusNotifier") as mock_notifier_cls:

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # Cache miss
        mock_redis_getter.return_value = mock_redis

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_result.scalars.return_value.first.return_value = job
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await processor.process_job("job-fa-ai-fallback")

        # English subtitles were downloaded and AI translation was invoked
        mock_dl_subs.assert_called_once()
        mock_ai_trans.assert_called_once()
        assert job.status == JobStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_persian_direct_failure_falls_back_to_ai(tmp_path):
    """If native Persian download fails after all retries, worker falls back to English + AI translation."""
    processor = JobProcessor()
    mock_bot = AsyncMock()
    processor.bot = mock_bot

    job = Job(
        id="job-fa-fail-retry",
        source_url="https://www.youtube.com/watch?v=fail_fa_vid",
        canonical_url="https://www.youtube.com/watch?v=fail_fa_vid",
        source_id="fail_fa_vid",
        title="Persian Download Error Video",
        operation=OperationType.SUBTITLE.value,
        subtitle_lang="FA",
        status=JobStatus.QUEUED.value,
    )

    info_with_persian = {
        "id": "fail_fa_vid",
        "subtitles": {"fa": [{"ext": "vtt"}]},
        "automatic_captions": {},
    }

    raw_en_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello World\n"
    translated_fa_srt = "1\n00:00:01,000 --> 00:00:03,000\nسلام دنیا\n"

    call_count = 0

    async def fake_download_subtitles(url, output_template, cookies_file=None, on_retry=None, langs=None):
        nonlocal call_count
        call_count += 1
        if langs and "fa" in langs:
            # Persian direct download fails
            raise RuntimeError("YouTube 404 or corrupted Persian stream")
        # English fallback succeeds
        out_file = output_template.replace("%(ext)s", "en.vtt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(raw_en_vtt)

    with patch("src.worker.processor.get_redis_client") as mock_redis_getter, \
         patch.object(SettingService, "load_public_settings_to_runtime", AsyncMock(return_value={"telegram_cache_channel_id": "-1001234567890", "telegram_api_mode": "local"})), \
         patch.object(YtDlpService, "extract_metadata", AsyncMock(return_value=info_with_persian)), \
         patch.object(YtDlpService, "download_subtitles", AsyncMock(side_effect=fake_download_subtitles)) as mock_dl_subs, \
         patch.object(AIService, "translate_english_to_persian", AsyncMock(return_value=(True, translated_fa_srt, None))) as mock_ai_trans, \
         patch.object(processor, "_send_media_to_cache", AsyncMock(return_value=(123, 500))), \
         patch("src.worker.processor.AsyncSessionLocal") as mock_session_cls, \
         patch("src.worker.processor.StatusNotifier") as mock_notifier_cls:

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # Cache miss
        mock_redis_getter.return_value = mock_redis

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_result.scalars.return_value.first.return_value = job
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await processor.process_job("job-fa-fail-retry")

        # 1st call for Persian failed, 2nd call for English succeeded
        assert call_count == 2
        mock_ai_trans.assert_called_once()
        assert job.status == JobStatus.COMPLETED.value
