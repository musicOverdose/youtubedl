from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.database import get_db
from src.services.ai_service import AIService
from src.services.audit_service import AuditService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/ai", tags=["AI Translation"])


class AISettingsRequest(BaseModel):
    enabled: bool
    provider: str
    base_url: str
    model: str
    api_key: Optional[str] = None  # If None, keep existing key


from src.services.setting_service import SettingService


@router.get("")
async def get_ai_settings(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    return await SettingService.get_ai_settings(session)


@router.post("")
async def update_ai_settings(
    req: AISettingsRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    res = await SettingService.save_ai_settings(req.model_dump(), session)
    await AuditService.log_action(
        session,
        "AI_UPDATE",
        admin["sub"],
        f"Updated AI settings: Provider={req.provider}, Model={req.model}, Enabled={req.enabled}",
    )
    return res


@router.post("/test")
async def test_ai_connection(admin: dict = Depends(get_current_admin)):
    if not AIService.is_configured():
        raise HTTPException(
            status_code=400,
            detail="AI is not properly configured. Check enabled, base URL, API key, and model.",
        )

    sample_srt = (
        "1\n00:00:01,000 --> 00:00:03,000\nHello, welcome to this video!\n\n"
        "2\n00:00:03,500 --> 00:00:05,500\nToday we learn how to download subtitles.\n"
    )

    ok, result_srt, err = await AIService.translate_english_to_persian(sample_srt)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Translation test failed: {err}")

    return {
        "success": True,
        "message": "AI translation test successful! SRT format and timestamps preserved.",
        "sample_output": result_srt,
    }
