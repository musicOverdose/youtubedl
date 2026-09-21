import asyncio
import errno
import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar
from src.core.config import settings
from src.core.logger import setup_logger
from src.core.redis import get_redis_client

logger = setup_logger("ytdlp_service")


def _is_readonly_or_permission_error(exc: Exception) -> bool:
    """Checks if an exception is due to a read-only filesystem or restricted file permissions."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
            return True
        msg = str(exc).lower()
        if "read-only" in msg or "permission denied" in msg or "operation not permitted" in msg:
            return True
    return False


# Monkey-patch yt-dlp cookie saving to gracefully handle read-only cookie files/filesystems
# (e.g., in Docker containers where cookie secrets are mounted :ro to protect credentials from mutation).
_orig_cookiejar_save = YoutubeDLCookieJar.save


def _safe_cookiejar_save(self, filename=None, *args, **kwargs):
    try:
        return _orig_cookiejar_save(self, filename, *args, **kwargs)
    except OSError as e:
        if _is_readonly_or_permission_error(e):
            logger.debug(
                "Suppressed cookiejar save on read-only cookiefile (%s): %s",
                filename or getattr(self, "filename", None),
                e,
            )
            return None
        logger.warning(
            "Suppressed unexpected cookiejar save error on cookiefile (%s): %s",
            filename or getattr(self, "filename", None),
            e,
        )
        return None
    except Exception as e:
        logger.warning("Suppressed unexpected error during cookiejar save: %s", e)
        return None


YoutubeDLCookieJar.save = _safe_cookiejar_save

_orig_ydl_save_cookies = yt_dlp.YoutubeDL.save_cookies


def _safe_ydl_save_cookies(self):
    try:
        return _orig_ydl_save_cookies(self)
    except OSError as e:
        if _is_readonly_or_permission_error(e):
            logger.debug("Suppressed save_cookies on read-only cookiefile: %s", e)
            return None
        logger.warning("Suppressed unexpected save_cookies error on cookiefile: %s", e)
        return None
    except Exception as e:
        logger.warning("Suppressed unexpected error during save_cookies: %s", e)
        return None


yt_dlp.YoutubeDL.save_cookies = _safe_ydl_save_cookies


class YouTubeSubtitleRateLimitError(Exception):
    """Raised when YouTube HTTP 429 rate limit persists across all retries."""
    pass


def is_http_429_error(exc: Exception) -> bool:
    """Checks whether the exception is specifically an HTTP 429 Too Many Requests error."""
    msg = str(exc).lower()
    return "429" in msg and (
        "too many requests" in msg
        or "http error 429" in msg
        or "http error: 429" in msg
        or "status 429" in msg
    )


SUBTITLE_COOLDOWN_KEY = "ytdlp:subtitles:cooldown"
_in_memory_subtitle_cooldown_until: float = 0.0


async def get_subtitle_cooldown_remaining() -> float:
    """Returns remaining seconds of global subtitle cooldown, or 0.0 if not active."""
    global _in_memory_subtitle_cooldown_until
    try:
        r = get_redis_client()
        ttl = await r.ttl(SUBTITLE_COOLDOWN_KEY)
        if ttl and ttl > 0:
            return float(ttl)
    except Exception:
        pass
    now = time.monotonic()
    rem = _in_memory_subtitle_cooldown_until - now
    return max(0.0, rem)


async def set_subtitle_cooldown(cooldown_seconds: int = 30) -> None:
    """Sets a short global cooldown after encountering HTTP 429 to protect the IP."""
    global _in_memory_subtitle_cooldown_until
    try:
        r = get_redis_client()
        await r.set(SUBTITLE_COOLDOWN_KEY, "1", ex=cooldown_seconds)
    except Exception:
        pass
    now = time.monotonic()
    _in_memory_subtitle_cooldown_until = max(_in_memory_subtitle_cooldown_until, now + cooldown_seconds)


# Standard YouTube URL regex patterns
YOUTUBE_URL_REGEX = re.compile(
    r"^(https?://)?(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)([\w-]{11})([^\s]*)?$",
    re.IGNORECASE,
)


def extract_youtube_id(url: str) -> Optional[str]:
    match = YOUTUBE_URL_REGEX.match(url.strip())
    if match:
        return match.group(5)
    return None


def get_canonical_url(source_id: str) -> str:
    return f"https://www.youtube.com/watch?v={source_id}"


_metadata_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_METADATA_CACHE_TTL: float = 300.0  # 5 minutes


class YtDlpService:
    @staticmethod
    def get_base_opts(cookies_file: Optional[str] = None) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "no_color": True,
            "ignoreerrors": False,
            "noplaylist": not settings.PLAYLISTS_ENABLED,
            "socket_timeout": 30,
        }

        active_cookie_file = cookies_file or settings.YTDLP_COOKIES_FILE
        if settings.YTDLP_COOKIES_ENABLED and active_cookie_file and os.path.exists(active_cookie_file):
            opts["cookiefile"] = active_cookie_file

        if settings.YTDLP_PROXY:
            opts["proxy"] = settings.YTDLP_PROXY

        return opts

    @classmethod
    async def extract_metadata(
        cls, url: str, cookies_file: Optional[str] = None, use_cache: bool = True
    ) -> Dict[str, Any]:
        """Extract full metadata without downloading, using a short-lived cache when available."""
        source_id = extract_youtube_id(url)
        now = time.monotonic()

        if use_cache and source_id and source_id in _metadata_cache:
            ts, cached_info = _metadata_cache[source_id]
            if now - ts < _METADATA_CACHE_TTL:
                return cached_info

        loop = asyncio.get_running_loop()
        opts = cls.get_base_opts(cookies_file)

        def _extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await loop.run_in_executor(None, _extract)

        if source_id and info:
            # Clean expired items if cache grows
            if len(_metadata_cache) > 50:
                expired = [k for k, (t, _) in _metadata_cache.items() if now - t >= _METADATA_CACHE_TTL]
                for k in expired:
                    _metadata_cache.pop(k, None)
            _metadata_cache[source_id] = (now, info)

        return info

    @classmethod
    def get_available_resolutions(cls, info: Dict[str, Any]) -> List[int]:
        """
        Inspect yt-dlp format metadata and extract actual unique available video heights.
        Sorts descending: e.g. [2160, 1440, 1080, 720, 480, 360, 240, 144].
        """
        formats = info.get("formats", [])
        heights = set()

        for f in formats:
            # Must have a video stream
            vcodec = f.get("vcodec")
            if not vcodec or vcodec == "none":
                continue

            height = f.get("height")
            if height and isinstance(height, int) and height > 0:
                heights.add(height)

        sorted_heights = sorted(list(heights), reverse=True)
        return sorted_heights

    @classmethod
    def get_available_codecs_for_height(cls, info: Dict[str, Any], target_height: int) -> List[str]:
        """
        Inspects yt-dlp format metadata for the exact target_height.
        Returns a list of available codecs from ["H264", "H265"] that actually exist at that height.
        Recognizes:
        - H.264: avc1*, h264*
        - H.265/HEVC: hev1*, hvc1*, hevc*, h265*
        Formats with vcodec == 'none' or non-matching heights are ignored.
        """
        formats = info.get("formats", [])
        codecs = set()

        for f in formats:
            h = f.get("height")
            if h != target_height:
                continue

            vcodec = f.get("vcodec")
            if not vcodec or vcodec == "none":
                continue

            vcodec_lower = str(vcodec).lower()
            if vcodec_lower.startswith("avc1") or vcodec_lower.startswith("h264"):
                codecs.add("H264")
            elif (
                vcodec_lower.startswith("hev1")
                or vcodec_lower.startswith("hvc1")
                or vcodec_lower.startswith("hevc")
                or vcodec_lower.startswith("h265")
            ):
                codecs.add("H265")

        res = []
        if "H264" in codecs:
            res.append("H264")
        if "H265" in codecs:
            res.append("H265")
        return res

    @classmethod
    def check_english_subtitles(cls, info: Dict[str, Any]) -> Tuple[bool, Optional[str], bool]:
        """
        Returns (has_english, track_key, is_auto_generated).
        Normalizes en, en-US, en-GB, en-orig, etc.
        """
        # 1. Check manual subtitles first
        subtitles = info.get("subtitles") or {}
        for key in subtitles:
            if key == "en" or key.startswith("en-") or key.startswith("en_"):
                return True, key, False

        # 2. Check automatic captions
        auto_caps = info.get("automatic_captions") or {}
        for key in auto_caps:
            if key == "en" or key.startswith("en-") or key.startswith("en_"):
                return True, key, True

        return False, None, False

    @classmethod
    def validate_duration(
        cls, info: Dict[str, Any], max_duration_seconds: int, allow_unknown: bool
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Validates duration:
        - If duration is None or <= 0: check allow_unknown.
        - If duration > max_duration_seconds: rejected.
        - If duration <= max_duration_seconds: accepted.
        Returns: (is_valid, duration_seconds, error_reason)
        """
        duration = info.get("duration")
        if duration is None or duration <= 0:
            if allow_unknown:
                return True, None, None
            return False, None, "Unknown video duration is not permitted."

        if duration > max_duration_seconds:
            max_formatted = cls.format_duration(max_duration_seconds)
            actual_formatted = cls.format_duration(int(duration))
            return (
                False,
                int(duration),
                f"❌ This video is too long.\nMaximum allowed: {max_formatted}\nVideo duration: {actual_formatted}",
            )

        return True, int(duration), None

    @staticmethod
    def format_duration(seconds: int) -> str:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @classmethod
    def build_video_format_spec(cls, target_height: int, target_codec: Optional[str] = None) -> str:
        """
        STRICT EXACT QUALITY & CODEC:
        Ensures height == target_height AND video codec matches target_codec.
        - H264: vcodec~='(?i)^(avc1|h264)'
        - H265: vcodec~='(?i)^(hev1|hvc1|hevc|h265)'
        Prefers compatible AAC audio (ext=m4a) for zero-conversion muxing,
        falling back to best audio.
        NEVER falls back to a different video codec or resolution!
        """
        if not target_codec:
            return f"bestvideo[height={target_height}]+bestaudio/best[height={target_height}]"

        codec_upper = target_codec.upper()
        if codec_upper == "H264":
            vfilter = "vcodec~='(?i)^(avc1|h264)'"
        elif codec_upper == "H265":
            vfilter = "vcodec~='(?i)^(hev1|hvc1|hevc|h265)'"
        else:
            raise ValueError(f"Unsupported target codec: {target_codec}")

        return (
            f"bestvideo[height={target_height}][{vfilter}]+bestaudio[ext=m4a]/"
            f"bestvideo[height={target_height}][{vfilter}]+bestaudio/"
            f"best[height={target_height}][{vfilter}]"
        )

    @classmethod
    async def download_subtitles(
        cls,
        url: str,
        output_template: str,
        cookies_file: Optional[str] = None,
        on_retry: Optional[Callable[[int, float, Exception], Any]] = None,
    ) -> None:
        """
        Downloads English subtitles with conservative backoff specifically for HTTP 429:
        - Attempt 1: wait ~10s (+jitter)
        - Attempt 2: wait ~30s (+jitter)
        - Attempt 3: wait ~60s (+jitter)
        Only retries on HTTP 429. Unrelated failures raise immediately.
        Respects global subtitle cooldown across concurrent worker jobs.
        """
        # 1. Check & respect global subtitle cooldown if another job recently hit 429
        cd_rem = await get_subtitle_cooldown_remaining()
        if cd_rem > 0:
            logger.info("Global subtitle cooldown active (%0.1fs remaining), waiting before attempt...", cd_rem)
            await asyncio.sleep(cd_rem)

        backoff_delays = [10.0, 30.0, 60.0]
        max_retries = len(backoff_delays)

        opts = cls.get_base_opts(cookies_file)
        opts.update({
            "outtmpl": output_template,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en.*", "en"],
            "subtitlesformat": "srt/vtt/best",
        })

        loop = asyncio.get_running_loop()

        for attempt in range(max_retries + 1):
            try:
                def _dl():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])

                await loop.run_in_executor(None, _dl)
                logger.info("Subtitles downloaded successfully for %s on attempt %d", url, attempt + 1)
                return
            except Exception as e:
                # ONLY retry if error is specifically HTTP 429
                if not is_http_429_error(e):
                    logger.error("Subtitle download failed with non-429 error: %s", e)
                    raise

                logger.warning(
                    "YouTube subtitle download encountered HTTP 429 on attempt %d/%d: %s",
                    attempt + 1,
                    max_retries + 1,
                    e,
                )

                # Set global cooldown so concurrent jobs/workers back off this IP
                await set_subtitle_cooldown(cooldown_seconds=30)

                if attempt >= max_retries:
                    raise YouTubeSubtitleRateLimitError(
                        "YouTube rate-limited subtitle extraction (HTTP 429: Too Many Requests) across all retries. "
                        "Please try again later or configure YouTube Cookies or a Proxy in the Admin Panel."
                    ) from e

                base_delay = backoff_delays[attempt]
                jitter = random.uniform(0.5, 3.0)
                wait_time = base_delay + jitter

                logger.info(
                    "Waiting %0.1fs (base=%0.1fs, jitter=%0.1fs) before subtitle retry %d/%d",
                    wait_time,
                    base_delay,
                    jitter,
                    attempt + 1,
                    max_retries,
                )

                if on_retry:
                    try:
                        res = on_retry(attempt + 1, wait_time, e)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as cb_err:
                        logger.warning("on_retry callback failed: %s", cb_err)

                await asyncio.sleep(wait_time)
