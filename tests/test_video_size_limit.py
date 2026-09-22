import pytest
import yt_dlp
from unittest.mock import AsyncMock, MagicMock, patch
from src.core.config import settings
from src.core.constants import JobStatus, OperationType
from src.models.job import Job
from src.services.setting_service import SettingService
from src.services.ytdlp_service import YtDlpService
from src.worker.processor import JobProcessor


def test_exact_filesize_calculation():
    """Video + audio with known filesize values produces an EXACT size calculation."""
    info = {
        "id": "vid1",
        "duration": 600,
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 500 * 1024 * 1024,  # 500 MB
                "ext": "mp4",
            },
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 10 * 1024 * 1024,  # 10 MB
                "ext": "m4a",
            },
        ],
    }

    size_bytes, size_source, details = YtDlpService.calculate_expected_video_size(
        info, target_height=1080, target_codec="H264"
    )

    assert size_source == "exact"
    assert details["video_size"] == 500 * 1024 * 1024
    assert details["audio_final_size"] == 10 * 1024 * 1024
    assert details["audio_copied"] is True
    # Final size includes overhead
    overhead = int((500 * 1024 * 1024 + 10 * 1024 * 1024) * 0.005)
    assert size_bytes == 510 * 1024 * 1024 + overhead


def test_filesize_approx_calculation():
    """Video with filesize_approx produces an APPROXIMATE size calculation."""
    info = {
        "id": "vid2",
        "duration": 600,
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize_approx": 300 * 1024 * 1024,
                "ext": "mp4",
            },
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 10 * 1024 * 1024,
                "ext": "m4a",
            },
        ],
    }

    size_bytes, size_source, details = YtDlpService.calculate_expected_video_size(
        info, target_height=1080, target_codec="H264"
    )

    assert size_source == "approximate"
    assert details["video_size"] == 300 * 1024 * 1024
    assert details["audio_final_size"] == 10 * 1024 * 1024


def test_bitrate_fallback_calculation():
    """Formats with only bitrate (vbr/tbr) and duration produce an ESTIMATED size calculation."""
    duration = 1000  # seconds
    info = {
        "id": "vid3",
        "duration": duration,
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "vbr": 4000.0,  # 4000 kbps
                "ext": "mp4",
            },
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128.0,  # 128 kbps
                "ext": "m4a",
            },
        ],
    }

    size_bytes, size_source, details = YtDlpService.calculate_expected_video_size(
        info, target_height=1080, target_codec="H264"
    )

    assert size_source == "estimated"
    expected_v_bytes = int((4000.0 * 1000 / 8) * duration)
    expected_a_bytes = int((128.0 * 1000 / 8) * duration)
    assert details["video_size"] == expected_v_bytes
    assert details["audio_final_size"] == expected_a_bytes


def test_aac_stream_copy_preserves_audio_size():
    """AAC source audio stream is stream-copied without re-encoding."""
    info = {
        "id": "vid4",
        "duration": 500,
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 100 * 1024 * 1024,
                "ext": "mp4",
            },
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 8 * 1024 * 1024,
                "ext": "m4a",
            },
        ],
    }

    size_bytes, size_source, details = YtDlpService.calculate_expected_video_size(
        info, target_height=1080, target_codec="H264"
    )

    assert details["audio_copied"] is True
    assert details["audio_final_size"] == 8 * 1024 * 1024


def test_non_aac_audio_transcode_to_192kbps():
    """Non-AAC audio (e.g. Opus in WebM) is transcoded to AAC 192k; size is calculated from 192k * duration."""
    duration = 300  # 300 seconds
    info = {
        "id": "vid5",
        "duration": duration,
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 100 * 1024 * 1024,
                "ext": "mp4",
            },
            {
                "format_id": "251",
                "vcodec": "none",
                "acodec": "opus",
                "filesize": 20 * 1024 * 1024,  # Original Opus size is ignored
                "ext": "webm",
            },
        ],
    }

    size_bytes, size_source, details = YtDlpService.calculate_expected_video_size(
        info, target_height=1080, target_codec="H264"
    )

    assert details["audio_copied"] is False
    # audio_final_bytes = (192,000 / 8) * duration
    expected_audio_final = int((192000 / 8) * duration)
    assert details["audio_final_size"] == expected_audio_final
    assert size_source == "estimated"


def test_combined_video_audio_format():
    """Single format with both video and audio streams (e.g. legacy 720p mp4)."""
    info = {
        "id": "vid6",
        "duration": 120,
        "formats": [
            {
                "format_id": "22",
                "height": 720,
                "vcodec": "avc1.64001F",
                "acodec": "mp4a.40.2",
                "filesize": 50 * 1024 * 1024,
                "ext": "mp4",
            }
        ],
    }

    size_bytes, size_source, details = YtDlpService.calculate_expected_video_size(
        info, target_height=720, target_codec="H264"
    )

    assert size_source == "exact"
    assert details["video_size"] == 50 * 1024 * 1024
    assert details["audio_final_size"] == 0
    overhead = int(50 * 1024 * 1024 * 0.005)
    assert size_bytes == 50 * 1024 * 1024 + overhead


def test_format_helpers():
    """Verify human-readable format helpers."""
    assert YtDlpService.format_file_size(684 * 1024 * 1024) == "684 MB"
    assert YtDlpService.format_file_size(int(2.14 * (1024 ** 3))) == "2.14 GB"
    assert YtDlpService.format_mb(1900) == "1.9 GB"
    assert YtDlpService.format_mb(48) == "48 MB"
    assert YtDlpService.format_mb(2000) == "2 GB"
    assert YtDlpService.format_mb(50) == "50 MB"


def test_local_vs_cloud_limits():
    """Verifies limit retrieval based on active api_mode."""
    local_limit = SettingService.get_max_video_file_size_mb("local")
    cloud_limit = SettingService.get_max_video_file_size_mb("cloud")
    assert local_limit == 1900
    assert cloud_limit == 48


@pytest.mark.asyncio
async def test_worker_pre_download_rejects_oversized(tmp_path):
    """Worker pre-download check rejects oversized video before invoking yt_dlp download."""
    oversized_info = {
        "id": "big_vid",
        "duration": 3600,
        "title": "Huge Video",
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 2500 * 1024 * 1024,
                "ext": "mp4",
            },
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 50 * 1024 * 1024,
                "ext": "m4a",
            },
        ],
    }

    processor = JobProcessor()
    mock_bot = AsyncMock()
    processor.bot = mock_bot

    job = Job(
        id="job-big",
        source_url="https://www.youtube.com/watch?v=big_vid",
        canonical_url="https://www.youtube.com/watch?v=big_vid",
        source_id="big_vid",
        title="Huge Video",
        operation=OperationType.VIDEO.value,
        output_codec="H264",
        resolution="1080p",
        target_height=1080,
        status=JobStatus.QUEUED.value,
    )

    with patch.object(YtDlpService, "extract_metadata", AsyncMock(return_value=oversized_info)), \
         patch.object(SettingService, "load_public_settings_to_runtime", AsyncMock(return_value={"telegram_cache_channel_id": "-1001234567890", "telegram_api_mode": "local"})), \
         patch.object(yt_dlp.YoutubeDL, "download") as mock_ydl_dl, \
         patch("src.worker.processor.AsyncSessionLocal") as mock_session_cls, \
         patch("src.worker.processor.StatusNotifier") as mock_notifier_cls:

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_result.scalars.return_value.first.return_value = job
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await processor.process_job("job-big")

        # The real yt-dlp download must NEVER be called
        mock_ydl_dl.assert_not_called()
        assert job.status == JobStatus.FAILED.value
        assert job.error_code == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_worker_route_safety_rejects_large_file_on_cloud_mode():
    """A 1.2 GB file under Cloud mode is rejected with UPLOAD_ROUTE_UNAVAILABLE before downloading."""
    large_info = {
        "id": "med_vid",
        "duration": 1800,
        "title": "1.2 GB Video",
        "formats": [
            {
                "format_id": "137",
                "height": 1080,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 1200 * 1024 * 1024,  # 1.2 GB
                "ext": "mp4",
            },
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 20 * 1024 * 1024,
                "ext": "m4a",
            },
        ],
    }

    processor = JobProcessor()
    mock_bot = AsyncMock()
    processor.bot = mock_bot

    job = Job(
        id="job-med",
        source_url="https://www.youtube.com/watch?v=med_vid",
        canonical_url="https://www.youtube.com/watch?v=med_vid",
        source_id="med_vid",
        title="1.2 GB Video",
        operation=OperationType.VIDEO.value,
        output_codec="H264",
        resolution="1080p",
        target_height=1080,
        status=JobStatus.QUEUED.value,
    )

    with patch.object(YtDlpService, "extract_metadata", AsyncMock(return_value=large_info)), \
         patch.object(SettingService, "load_public_settings_to_runtime", AsyncMock(return_value={"telegram_cache_channel_id": "-1001234567890", "telegram_api_mode": "cloud"})), \
         patch.object(yt_dlp.YoutubeDL, "download") as mock_ydl_dl, \
         patch("src.worker.processor.AsyncSessionLocal") as mock_session_cls, \
         patch("src.worker.processor.StatusNotifier") as mock_notifier_cls:

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_result.scalars.return_value.first.return_value = job
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        mock_notifier = AsyncMock()
        mock_notifier_cls.return_value = mock_notifier

        await processor.process_job("job-med")

        mock_ydl_dl.assert_not_called()
        assert job.status == JobStatus.FAILED.value
        assert job.error_code == "UPLOAD_ROUTE_UNAVAILABLE"
