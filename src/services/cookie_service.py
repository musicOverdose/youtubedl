import os
import shutil
import tempfile
from typing import Dict, Optional, Tuple
from src.core.config import settings
from src.core.logger import setup_logger
from src.services.ytdlp_service import YtDlpService

logger = setup_logger("cookie_service")


class CookieService:
    @staticmethod
    def validate_netscape_format(content: str) -> Tuple[bool, int, Optional[str]]:
        """
        Validates standard Netscape cookies.txt format.
        Netscape format requires 7 tab-separated fields:
        domain, flag (TRUE/FALSE), path, secure (TRUE/FALSE), expiration, name, value.
        """
        lines = content.splitlines()
        cookie_count = 0

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue

            # Strip optional leading #HttpOnly_
            clean_line = line
            if clean_line.startswith("#HttpOnly_"):
                clean_line = clean_line[len("#HttpOnly_") :]

            fields = clean_line.split("\t")
            if len(fields) != 7:
                return (
                    False,
                    0,
                    f"Line {line_num} does not contain 7 tab-separated fields required by Netscape format.",
                )

            # Validate flags are TRUE/FALSE
            if fields[1].upper() not in ("TRUE", "FALSE"):
                return False, 0, f"Line {line_num}: include_subdomains flag must be TRUE or FALSE."

            cookie_count += 1

        if cookie_count == 0:
            return False, 0, "No valid cookies found in provided content."

        return True, cookie_count, None

    @classmethod
    def save_cookies_atomically(cls, content: str) -> Tuple[bool, int, Optional[str]]:
        """
        Validates content and atomically writes to target cookies file.
        """
        is_valid, count, err = cls.validate_netscape_format(content)
        if not is_valid:
            return False, 0, err

        target_file = settings.YTDLP_COOKIES_FILE
        target_dir = os.path.dirname(target_file)
        os.makedirs(target_dir, exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix="cookies_", suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, target_file)
            logger.info(f"Successfully saved {count} cookies atomically to {target_file}")
            return True, count, None
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            logger.error(f"Failed to write cookies file: {e}")
            return False, 0, f"Failed to save cookies: {e}"

    @classmethod
    def delete_cookies(cls) -> bool:
        """Removes the active cookies file."""
        target_file = settings.YTDLP_COOKIES_FILE
        if os.path.exists(target_file):
            try:
                os.remove(target_file)
                logger.info("Cookies file removed successfully")
                return True
            except Exception as e:
                logger.error(f"Error removing cookies file: {e}")
                return False
        return True

    @classmethod
    def get_status(cls) -> Dict:
        """Returns non-sensitive metadata about configured cookies."""
        target_file = settings.YTDLP_COOKIES_FILE
        exists = os.path.exists(target_file)
        cookie_count = 0
        last_updated = None

        if exists:
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    content = f.read()
                _, cookie_count, _ = cls.validate_netscape_format(content)
                mtime = os.path.getmtime(target_file)
                import datetime
                last_updated = datetime.datetime.fromtimestamp(
                    mtime, tz=datetime.timezone.utc
                ).isoformat()
            except Exception:
                pass

        return {
            "configured": exists and cookie_count > 0,
            "enabled": settings.YTDLP_COOKIES_ENABLED,
            "cookie_count": cookie_count,
            "last_updated": last_updated,
        }

    @classmethod
    async def test_cookies(cls, test_url: str) -> Tuple[bool, str]:
        """Tests active cookies by running metadata extraction on a YouTube URL."""
        target_file = settings.YTDLP_COOKIES_FILE
        if not os.path.exists(target_file):
            return False, "No cookies file is configured."

        try:
            info = await YtDlpService.extract_metadata(test_url, cookies_file=target_file)
            title = info.get("title", "Unknown")
            return True, f"Authentication succeeded! Extracted video: '{title}'"
        except Exception as e:
            logger.warning(f"Cookie test failed: {e}")
            return False, f"Authentication/Extraction failed: {str(e)[:200]}"
