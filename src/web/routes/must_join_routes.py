from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.bot.bot_instance import get_bot
from src.core.config import settings
from src.core.database import get_db
from src.models.channel import RequiredChannel
from src.services.audit_service import AuditService
from src.services.must_join_service import MustJoinService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/must-join", tags=["Must Join"])


class AddChannelRequest(BaseModel):
    chat_id: int
    title: str
    username: Optional[str] = None
    invite_url: Optional[str] = None
    enabled: bool = True


class UpdateChannelRequest(BaseModel):
    title: str
    username: Optional[str] = None
    invite_url: Optional[str] = None
    enabled: bool = True


class ToggleRequest(BaseModel):
    enabled: bool


@router.get("")
async def get_must_join_info(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).order_by(RequiredChannel.created_at.asc())
    res = await session.execute(stmt)
    channels = res.scalars().all()

    from src.services.setting_service import SettingService
    exempt_users = await SettingService.get_must_join_exempt_users(session)

    return {
        "enabled": settings.MUST_JOIN_ENABLED,
        "exempt_users": exempt_users,
        "channels": [
            {
                "id": c.id,
                "chat_id": c.chat_id,
                "title": c.title,
                "username": c.username,
                "invite_url": c.invite_url,
                "enabled": c.enabled,
                "bot_status": c.bot_status,
                "last_bot_check": c.last_bot_check.isoformat() if c.last_bot_check else None,
                "last_error": c.last_error,
            }
            for c in channels
        ],
    }


from src.services.setting_service import SETTING_MUST_JOIN_ENABLED, SettingService


@router.post("/toggle")
async def toggle_must_join(
    req: ToggleRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    await SettingService.save_single_setting(
        SETTING_MUST_JOIN_ENABLED, req.enabled, description="Must Join System Enabled", session=session
    )
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], f"Must Join system toggled to {req.enabled}"
    )
    return {"status": "ok", "must_join_enabled": settings.MUST_JOIN_ENABLED}


@router.post("/channels")
async def add_channel(
    req: AddChannelRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    bot = get_bot()
    is_admin, status_detail = await MustJoinService.verify_bot_is_admin(bot, req.chat_id)
    if not is_admin:
        raise HTTPException(
            status_code=400,
            detail=f"❌ Bot is not an administrator in this channel: {status_detail}",
        )

    # Check for existing
    stmt = select(RequiredChannel).where(RequiredChannel.chat_id == req.chat_id)
    res = await session.execute(stmt)
    existing = res.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Channel with this Chat ID already exists")

    ch = RequiredChannel(
        chat_id=req.chat_id,
        title=req.title,
        username=req.username.lstrip("@") if req.username else None,
        invite_url=req.invite_url,
        enabled=req.enabled,
        bot_status="administrator" if is_admin else "not_admin",
        last_bot_check=datetime.now(timezone.utc),
    )
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    await AuditService.log_action(
        session, "CHANNEL_ADD", admin["sub"], f"Added required channel {req.title} ({req.chat_id})"
    )
    return {"status": "added", "channel_id": ch.id}


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    req: UpdateChannelRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).where(RequiredChannel.id == channel_id)
    res = await session.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    ch.title = req.title
    ch.username = req.username.lstrip("@") if req.username else None
    ch.invite_url = req.invite_url
    ch.enabled = req.enabled
    await session.commit()

    await AuditService.log_action(
        session, "CHANNEL_UPDATE", admin["sub"], f"Updated required channel {ch.id}"
    )
    return {"status": "updated"}


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: int,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).where(RequiredChannel.id == channel_id)
    res = await session.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    await session.delete(ch)
    await session.commit()
    await AuditService.log_action(
        session, "CHANNEL_REMOVE", admin["sub"], f"Removed required channel {channel_id}"
    )
    return {"status": "deleted"}


@router.post("/channels/{channel_id}/test")
async def test_channel_bot_status(
    channel_id: int,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).where(RequiredChannel.id == channel_id)
    res = await session.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    bot = get_bot()
    is_admin, status_detail = await MustJoinService.verify_bot_is_admin(bot, ch.chat_id)
    ch.bot_status = "administrator" if is_admin else "not_admin"
    ch.last_bot_check = datetime.now(timezone.utc)
    ch.last_error = None if is_admin else status_detail
    await session.commit()

    return {
        "is_admin": is_admin,
        "detail": status_detail,
        "bot_status": ch.bot_status,
    }


class MustJoinMessageRequest(BaseModel):
    message: str


@router.get("/message")
async def get_must_join_message(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import DEFAULT_MUST_JOIN_MESSAGE, SettingService
    msg = await SettingService.get_must_join_message(session)
    return {
        "message": msg,
        "is_default": (msg == DEFAULT_MUST_JOIN_MESSAGE),
        "default_message": DEFAULT_MUST_JOIN_MESSAGE,
    }


@router.post("/message")
async def update_must_join_message(
    req: MustJoinMessageRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    saved_msg = await SettingService.save_must_join_message(
        req.message, session=session, admin_username=admin.get("sub", "admin")
    )
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], "Updated custom Must-Join message template"
    )
    return {"status": "saved", "message": saved_msg}


@router.post("/message/reset")
async def reset_must_join_message(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    reset_msg = await SettingService.reset_must_join_message(
        session=session, admin_username=admin.get("sub", "admin")
    )
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], "Reset Must-Join message template to default"
    )
    return {"status": "reset", "message": reset_msg}


class ExemptUsersRequest(BaseModel):
    exempt_users: str


@router.get("/exempt-users")
async def get_must_join_exempt_users(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    val = await SettingService.get_must_join_exempt_users(session)
    return {"exempt_users": val}


@router.post("/exempt-users")
async def save_must_join_exempt_users(
    req: ExemptUsersRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    saved = await SettingService.save_must_join_exempt_users(req.exempt_users, session=session)
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], f"Updated Must-Join exempt users list: {saved[:100]}"
    )
    return {"status": "saved", "exempt_users": saved}

