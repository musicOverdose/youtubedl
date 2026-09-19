from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import get_db
from src.models.audit import AuditLog
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/audit", tags=["Audit"])


@router.get("")
async def list_audit_logs(
    action: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))

    if action:
        query = query.where(AuditLog.action == action)
        count_query = count_query.where(AuditLog.action == action)

    total = await session.scalar(count_query) or 0
    query = query.order_by(desc(AuditLog.created_at)).offset(offset).limit(limit)
    res = await session.execute(query)
    logs = res.scalars().all()

    items = [
        {
            "id": l.id,
            "action": l.action,
            "admin_username": l.admin_username,
            "details": l.details,
            "ip_address": l.ip_address,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]

    return {"total": total, "items": items, "limit": limit, "offset": offset}
