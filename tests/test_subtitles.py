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
