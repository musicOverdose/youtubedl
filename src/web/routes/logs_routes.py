from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/logs", tags=["Logs"])

# Ring buffer for recent in-memory log entries
LOG_BUFFER: List[dict] = []
MAX_BUFFER_SIZE = 500


def record_log_entry(level: str, service: str, message: str):
    from datetime import datetime, timezone
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "service": service,
        "message": message,
    }
    LOG_BUFFER.append(entry)
    if len(LOG_BUFFER) > MAX_BUFFER_SIZE:
        LOG_BUFFER.pop(0)


@router.get("")
async def get_recent_logs(
    level: Optional[str] = None,
    service: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    admin: dict = Depends(get_current_admin),
):
    filtered = LOG_BUFFER
    if level:
        filtered = [l for l in filtered if l["level"].upper() == level.upper()]
    if service:
        filtered = [l for l in filtered if service.lower() in l["service"].lower()]

    return {"logs": list(reversed(filtered[-limit:]))}
