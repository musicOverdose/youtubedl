import asyncio
import os
import shutil
import sys
from typing import Dict, Optional, Tuple
import psutil
import yt_dlp
from src.core.config import settings
from src.core.logger import setup_logger
from src.core.redis import get_redis_client

logger = setup_logger("system_service")


class SystemService:
    @classmethod
    def get_system_stats_sync(cls) -> Dict:
        """Synchronous collection of local CPU, RAM, and Disk metrics (used for fast worker checks)."""
        cpu_pct = psutil.cpu_percent(interval=0.0)
        mem = psutil.virtual_memory()

        temp_dir = settings.TEMP_DIR
        os.makedirs(temp_dir, exist_ok=True)
        disk = shutil.disk_usage(temp_dir)

        temp_used_bytes = 0
        try:
            for root, _, files in os.walk(temp_dir):
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
            "disk_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
            "temp_used_gb": round(temp_used_bytes / (1024**3), 3),
        }

    @classmethod
    async def get_system_stats(cls) -> Dict:
        """
        Collects CPU, RAM, and Disk metrics, integrating service-reported Redis telemetry
        from Worker and Local Bot API telemetry sidecar.
        """
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()

        worker_temp_bytes = 0
        worker_transfer_bytes = 0
        bot_api_data_bytes = 0
        bot_api_temp_bytes = 0
        host_free_bytes = None
        host_total_bytes = None
        host_used_bytes = None

        try:
            r = get_redis_client()
            t_temp = await r.get("telemetry:worker:temp_ytdl_bytes")
            if t_temp:
                worker_temp_bytes = int(t_temp)
            t_trans = await r.get("telemetry:worker:transfer_bytes")
            if t_trans:
                worker_transfer_bytes = int(t_trans)
            t_data = await r.get("telemetry:local_bot_api:data_bytes")
            if t_data:
                bot_api_data_bytes = int(t_data)
            t_api_temp = await r.get("telemetry:local_bot_api:temp_bytes")
            if t_api_temp:
                bot_api_temp_bytes = int(t_api_temp)
            h_free = await r.get("telemetry:host:filesystem_free_bytes")
            if h_free:
                host_free_bytes = int(h_free)
            h_tot = await r.get("telemetry:host:filesystem_total_bytes")
            if h_tot:
                host_total_bytes = int(h_tot)
            h_used = await r.get("telemetry:host:filesystem_used_bytes")
            if h_used:
                host_used_bytes = int(h_used)
        except Exception as e:
            logger.debug("Redis telemetry read exception: %s", e)

        # Fallback disk usage from local filesystem if host telemetry not available
        if host_free_bytes is None:
            disk = shutil.disk_usage(settings.TEMP_DIR if os.path.exists(settings.TEMP_DIR) else "/")
            disk_total = disk.total
            disk_free = disk.free
            disk_used = disk.used
        else:
            disk_total = host_total_bytes or 1
            disk_free = host_free_bytes
            disk_used = host_used_bytes or (disk_total - disk_free)

        # If worker telemetry is 0, check local temp directory if it exists
        if worker_temp_bytes == 0 and os.path.exists(settings.TEMP_DIR):
            try:
                for root, _, files in os.walk(settings.TEMP_DIR):
                    for f in files:
                        fp = os.path.join(root, f)
                        if os.path.exists(fp) and not os.path.islink(fp):
                            worker_temp_bytes += os.path.getsize(fp)
            except Exception:
                pass

        total_temp_gb = round(
            (worker_temp_bytes + worker_transfer_bytes + bot_api_temp_bytes) / (1024**3), 3
        )

        return {
            "cpu_percent": cpu_pct,
            "ram_total_gb": round(mem.total / (1024**3), 2),
            "ram_used_gb": round(mem.used / (1024**3), 2),
            "ram_percent": mem.percent,
            "disk_total_gb": round(disk_total / (1024**3), 2),
            "disk_used_gb": round(disk_used / (1024**3), 2),
            "disk_free_gb": round(disk_free / (1024**3), 2),
            "disk_percent": round((disk_used / disk_total) * 100, 1) if disk_total else 0,
            "temp_used_gb": total_temp_gb,
            "worker_temp_gb": round(worker_temp_bytes / (1024**3), 3),
            "worker_transfer_gb": round(worker_transfer_bytes / (1024**3), 3),
            "bot_api_data_gb": round(bot_api_data_bytes / (1024**3), 3),
            "bot_api_temp_gb": round(bot_api_temp_bytes / (1024**3), 3),
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
        stats = cls.get_system_stats_sync()
        if stats["temp_used_gb"] >= settings.MAX_TEMP_STORAGE_GB:
            return False, f"Temporary storage limit exceeded ({stats['temp_used_gb']} GB >= {settings.MAX_TEMP_STORAGE_GB} GB limit)."

        if stats["disk_free_gb"] < 2.0:
            return False, f"VPS filesystem free disk space is critically low ({stats['disk_free_gb']} GB remaining)."

        return True, None
