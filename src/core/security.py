from datetime import datetime, timedelta, timezone
from typing import Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt
from src.core.config import settings

ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

ALGORITHM = "HS256"
SESSION_EXPIRE_HOURS = 24


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return ph.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_session_token(subject: str, role: str = "ADMIN") -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=SESSION_EXPIRE_HOURS)
    to_encode = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_session_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None
