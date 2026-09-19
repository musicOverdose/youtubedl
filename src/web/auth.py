from typing import Optional
from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer
from src.core.config import settings
from src.core.security import decode_session_token, verify_password

security_bearer = HTTPBearer(auto_error=False)


async def get_current_admin(
    request: Request,
    session_token: Optional[str] = Cookie(None, alias="session_token"),
    bearer_token=Depends(security_bearer),
) -> dict:
    """Dependency that ensures the caller is an authenticated admin."""
    token = session_token
    if not token and bearer_token:
        token = bearer_token.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    payload = decode_session_token(token)
    if not payload or payload.get("role") != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        )

    return payload
