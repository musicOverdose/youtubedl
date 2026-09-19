import pytest
from src.services.ffmpeg_service import FFmpegService
from src.services.ytdlp_service import YtDlpService


def test_no_quality_fallback():
    # Source only has 720p and 360p
    mock_info = {
        "formats": [
            {"vcodec": "avc1.4d401f", "height": 720},
            {"vcodec": "avc1.4d401e", "height": 360},
        ]
    }
    available = YtDlpService.get_available_resolutions(mock_info)
    assert available == [720, 360]

    # If user requests 1080p, it must NOT be accepted or substituted
    requested_height = 1080
    assert requested_height not in available

    # Fallback to 720p is strictly forbidden
    valid_exact = requested_height in available
    assert valid_exact is False


def test_stream_copy_vs_transcode_decision():
    # Case 1: Source is H.264 + AAC, requested is H264
    # Expected: Stream copy both video and audio (no re-encoding!)
    probe_h264_aac = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_h264_aac, "H264")
    assert copy_v is True
    assert copy_a is True

    # Case 2: Source is VP9 + Opus, requested is H264
    # Expected: Cannot stream copy (must transcode both)
    probe_vp9_opus = {
        "streams": [
            {"codec_type": "video", "codec_name": "vp9"},
            {"codec_type": "audio", "codec_name": "opus"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_vp9_opus, "H264")
    assert copy_v is False
    assert copy_a is False

    # Case 3: Source is HEVC + AAC, requested is H265
    # Expected: Stream copy both video and audio
    probe_hevc_aac = {
        "streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_hevc_aac, "H265")
    assert copy_v is True
    assert copy_a is True

    # Case 4: Source is H.264 + AAC, requested is H265
    # Expected: Video must be transcoded to H265, but AAC audio can be stream-copied
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_h264_aac, "H265")
    assert copy_v is False
    assert copy_a is True
