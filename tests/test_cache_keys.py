import pytest
from src.services.cache_service import CacheService


def test_cache_keys_determinism_and_uniqueness():
    source_id = "ABC123XYZ"

    # Determinism
    k1 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    k2 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    assert k1 == k2

    # Different Codecs must not collide
    k_h264 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    k_h265 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H265", height=1080)
    assert k_h264 != k_h265

    # Different Resolutions must not collide
    k_1080 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    k_720 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=720)
    assert k_1080 != k_720

    # Video vs MP3
    k_mp3 = CacheService.generate_cache_key(source_id, "AUDIO", codec="MP3")
    assert k_mp3 != k_1080

    # Subtitles: English vs Persian
    k_sub_en = CacheService.generate_cache_key(source_id, "SUBTITLE", subtitle_lang="EN")
    k_sub_fa = CacheService.generate_cache_key(source_id, "SUBTITLE", subtitle_lang="FA")
    assert k_sub_en != k_sub_fa
