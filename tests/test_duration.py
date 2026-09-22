import pytest
from src.services.ytdlp_service import YtDlpService


def test_duration_validation():
    # Long videos and unknown durations are never rejected by duration limits (size limits govern)
    info_under = {"duration": 3600}
    is_valid, dur, err = YtDlpService.validate_duration(info_under)
    assert is_valid is True
    assert dur == 3600
    assert err is None

    info_over = {"duration": 108000}  # 30 hours long video
    is_valid, dur, err = YtDlpService.validate_duration(info_over)
    assert is_valid is True
    assert dur == 108000
    assert err is None

    info_unknown = {"duration": None}
    is_valid, dur, err = YtDlpService.validate_duration(info_unknown)
    assert is_valid is True
    assert dur is None
    assert err is None


def test_format_duration():
    assert YtDlpService.format_duration(0) == "00:00:00"
    assert YtDlpService.format_duration(65) == "00:01:05"
    assert YtDlpService.format_duration(3665) == "01:01:05"
    assert YtDlpService.format_duration(7200) == "02:00:00"
