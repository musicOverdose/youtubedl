import pytest
from src.services.ytdlp_service import YtDlpService


def test_duration_validation():
    max_limit = 7200  # 02:00:00

    # 1. Under limit: allowed
    info_under = {"duration": 3600}
    is_valid, dur, err = YtDlpService.validate_duration(info_under, max_limit, allow_unknown=False)
    assert is_valid is True
    assert dur == 3600
    assert err is None

    # 2. Exact limit: allowed
    info_exact = {"duration": 7200}
    is_valid, dur, err = YtDlpService.validate_duration(info_exact, max_limit, allow_unknown=False)
    assert is_valid is True
    assert dur == 7200
    assert err is None

    # 3. Over limit: rejected with error text
    info_over = {"duration": 10800}  # 03:00:00
    is_valid, dur, err = YtDlpService.validate_duration(info_over, max_limit, allow_unknown=False)
    assert is_valid is False
    assert dur == 10800
    assert "❌ This video is too long" in err
    assert "02:00:00" in err
    assert "03:00:00" in err

    # 4. Unknown duration (None or 0)
    info_unknown = {"duration": None}
    # When allow_unknown is False
    is_valid, dur, err = YtDlpService.validate_duration(info_unknown, max_limit, allow_unknown=False)
    assert is_valid is False
    assert "Unknown video duration" in err

    # When allow_unknown is True
    is_valid, dur, err = YtDlpService.validate_duration(info_unknown, max_limit, allow_unknown=True)
    assert is_valid is True
    assert dur is None
    assert err is None


def test_format_duration():
    assert YtDlpService.format_duration(0) == "00:00:00"
    assert YtDlpService.format_duration(65) == "00:01:05"
    assert YtDlpService.format_duration(3665) == "01:01:05"
    assert YtDlpService.format_duration(7200) == "02:00:00"
