from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.database import get_db
from src.services.audit_service import AuditService
from src.services.cookie_service import CookieService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/cookies", tags=["Cookies"])


class PasteCookiesRequest(BaseModel):
    content: str


class ToggleCookiesRequest(BaseModel):
    enabled: bool


class TestCookiesRequest(BaseModel):
    url: str


@router.get("")
async def get_cookies_status(admin: dict = Depends(get_current_admin)):
    return CookieService.get_status()


from src.services.setting_service import SETTING_YTDLP_COOKIES_ENABLED, SettingService


@router.post("/toggle")
async def toggle_cookies(
    req: ToggleCookiesRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    await SettingService.save_single_setting(
        SETTING_YTDLP_COOKIES_ENABLED, req.enabled, description="YouTube Cookies Enabled", session=session
    )
    await AuditService.log_action(
        session, "COOKIES_UPDATE", admin["sub"], f"Cookies toggled to {req.enabled}"
    )
    return {"status": "ok", "enabled": settings.YTDLP_COOKIES_ENABLED}


@router.post("/paste")
async def paste_cookies(
    req: PasteCookiesRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    ok, count, err = CookieService.save_cookies_atomically(req.content)
    if not ok:
        raise HTTPException(status_code=400, detail=err or "Invalid cookies.txt format")

    await AuditService.log_action(
        session, "COOKIES_UPDATE", admin["sub"], f"Saved {count} cookies via paste"
    )
    return {"status": "ok", "cookie_count": count}


@router.post("/upload")
async def upload_cookies(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    content_bytes = await file.read()
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be utf-8 encoded text")

    ok, count, err = CookieService.save_cookies_atomically(content)
    if not ok:
        raise HTTPException(status_code=400, detail=err or "Invalid cookies.txt format")

    await AuditService.log_action(
        session, "COOKIES_UPDATE", admin["sub"], f"Uploaded {count} cookies from {file.filename}"
    )
    return {"status": "ok", "cookie_count": count}


@router.post("/test")
async def test_cookies(
    req: TestCookiesRequest,
    admin: dict = Depends(get_current_admin),
):
    success, message = await CookieService.test_cookies(req.url)
    return {"success": success, "message": message}


@router.delete("")
async def delete_cookies(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    deleted = CookieService.delete_cookies()
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete cookies file")

    await AuditService.log_action(session, "COOKIES_DELETE", admin["sub"], "Deleted cookies file")
    return {"status": "deleted"}
