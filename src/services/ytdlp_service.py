import asyncio
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
import yt_dlp
from src.core.config import settings
from src.core.logger import setup_logger

logger = setup_logger("ytdlp_service")

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
        cls, url: str, cookies_file: Optional[str] = None
    ) -> Dict[str, Any]:
        """Extract full metadata without downloading."""
        loop = asyncio.get_running_loop()
        opts = cls.get_base_opts(cookies_file)

        def _extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await loop.run_in_executor(None, _extract)
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
    def build_video_format_spec(cls, target_height: int) -> str:
        """
        STRICT EXACT QUALITY:
        Ensures height == target_height.
        No fallback to lower/higher resolutions.
        """
        return f"bestvideo[height={target_height}]+bestaudio/best[height={target_height}]"
