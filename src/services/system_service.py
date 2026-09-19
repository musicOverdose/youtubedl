import asyncio
import os
import shutil
import sys
from typing import Dict, Optional, Tuple
import psutil
import yt_dlp
from src.core.config import settings
from src.core.logger import setup_logger

logger = setup_logger("system_service")


class SystemService:
    @staticmethod
    def get_system_stats() -> Dict:
        """Collects CPU, RAM, and Disk metrics."""
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()

        # Check disk of temp path
        temp_dir = settings.TEMP_DIR
        os.makedirs(temp_dir, exist_ok=True)
        disk = shutil.disk_usage(temp_dir)

        # Calculate directory size of temp_dir
        temp_used_bytes = 0
        try:
            for root, dirs, files in os.walk(temp_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.exists(fp) and not os.path.islink(fp):
                        temp_used_bytes += os.path.getsize(fp)
        except Exception:
            pass

        return {
            "cpu_percent": cpu_pct,
            "ram_total_gb": round(mem.total / (1024**3), 2),
            "ram_used_gb": round(mem.used / (1024**3), 2),
            "ram_percent": mem.percent,
            "disk_total_gb": round(disk.total / (1024**3), 2),
            "disk_used_gb": round(disk.used / (1024**3), 2),
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "disk_percent": round((disk.used / disk.total) * 100, 1),
            "temp_used_gb": round(temp_used_bytes / (1024**3), 3),
        }

    @staticmethod
    async def get_tool_versions() -> Dict[str, str]:
        """Detects versions of yt-dlp, yt-dlp-ejs, ffmpeg, deno, and python."""
        versions = {
            "application": "1.0.0",
            "python": sys.version.split()[0],
            "ytdlp": getattr(yt_dlp.version, "__version__", "unknown"),
            "ytdlp_ejs": "unknown",
            "ffmpeg": "unknown",
            "deno": "unknown",
        }

        try:
            import yt_dlp_ejs
            versions["ytdlp_ejs"] = getattr(yt_dlp_ejs, "__version__", "installed")
        except Exception:
            pass

        # Check ffmpeg
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                first_line = stdout.decode().splitlines()[0]
                # e.g. "ffmpeg version 7.1-..."
                versions["ffmpeg"] = first_line.split()[2] if len(first_line.split()) > 2 else "installed"
        except Exception:
            pass

        # Check deno
        try:
            proc = await asyncio.create_subprocess_exec(
                "deno", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                first_line = stdout.decode().splitlines()[0]
                # e.g. "deno 2.0.0..."
                versions["deno"] = first_line.split()[1] if len(first_line.split()) > 1 else "installed"
        except Exception:
            pass

        return versions

    @classmethod
    def check_disk_safety(cls) -> Tuple[bool, Optional[str]]:
        """
        Guarantees that free disk space and temp usage stay within safe limits:
        MAX_TEMP_STORAGE_GB (default 30 GB).
        """
        stats = cls.get_system_stats()
        if stats["temp_used_gb"] >= settings.MAX_TEMP_STORAGE_GB:
            return False, f"Temporary storage limit exceeded ({stats['temp_used_gb']} GB >= {settings.MAX_TEMP_STORAGE_GB} GB limit)."

        if stats["disk_free_gb"] < 2.0:  # Absolute safety margin: 2GB free
            return False, f"VPS filesystem free disk space is critically low ({stats['disk_free_gb']} GB remaining)."

        return True, None
