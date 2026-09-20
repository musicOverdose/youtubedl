from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.database import get_db
from src.core.security import create_session_token, hash_password, verify_password
from src.services.audit_service import AuditService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(req: LoginRequest, response: Response, session: AsyncSession = Depends(get_db)):
    from src.services.auth_service import AuthService

    authenticated = await AuthService.authenticate_admin(req.username, req.password, session=session)
    if not authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token = create_session_token(subject=req.username, role="ADMIN")
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to True if behind HTTPS proxy
        max_age=86400,
    )

    await AuditService.log_action(
        session, "LOGIN", req.username, "Successful web admin login"
    )

    return {"status": "ok", "username": req.username, "token": token}


@router.post("/logout")
async def logout(response: Response, current_user: dict = Depends(get_current_admin)):
    response.delete_cookie(key="session_token")
    return {"status": "logged_out"}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_admin)):
    return {
        "username": current_user.get("sub"),
        "role": current_user.get("role"),
    }
