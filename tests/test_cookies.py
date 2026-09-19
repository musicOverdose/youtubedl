import os
import pytest
from src.services.cookie_service import CookieService


def test_cookie_validation():
    valid_netscape = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1780000000\tVISITOR_INFO1_LIVE\tsome_value\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1780000000\tLOGIN_INFO\tanother_value\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1780000000\tSID\tsecure_sid\n"
    )

    is_valid, count, err = CookieService.validate_netscape_format(valid_netscape)
    assert is_valid is True
    assert count == 3
    assert err is None

    # Corrupted / invalid cookies (not enough fields)
    invalid_content = "this is just a random text string not a netscape cookie"
    is_valid, count, err = CookieService.validate_netscape_format(invalid_content)
    assert is_valid is False
    assert count == 0
    assert err is not None


def test_atomic_cookie_saving(tmp_path, monkeypatch):
    test_cookies_file = str(tmp_path / "secrets" / "youtube-cookies.txt")
    monkeypatch.setattr("src.core.config.settings.YTDLP_COOKIES_FILE", test_cookies_file)

    valid_netscape = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1780000000\tTEST_COOKIE\tsecret_123\n"
    )

    ok, count, err = CookieService.save_cookies_atomically(valid_netscape)
    assert ok is True
    assert count == 1
    assert os.path.exists(test_cookies_file)

    # Verify status masking (does NOT return secret values)
    status = CookieService.get_status()
    assert status["configured"] is True
    assert status["cookie_count"] == 1
    assert "secret_123" not in str(status)

    # Delete
    deleted = CookieService.delete_cookies()
    assert deleted is True
    assert not os.path.exists(test_cookies_file)
