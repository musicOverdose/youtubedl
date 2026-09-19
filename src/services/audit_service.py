from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.logger import setup_logger
from src.models.audit import AuditLog

logger = setup_logger("audit_service")


class AuditService:
    @staticmethod
    async def log_action(
        session: AsyncSession,
        action: str,
        admin_username: str,
        details: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> AuditLog:
        entry = AuditLog(
            action=action,
            admin_username=admin_username,
            details=details,
            ip_address=ip_address,
            created_at=datetime.now(timezone.utc),
        )
        session.add(entry)
        await session.commit()
        logger.info(f"AUDIT: [{admin_username}] {action} - {details}")
        return entry
