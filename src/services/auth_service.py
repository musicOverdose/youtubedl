import asyncio
import os
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.core.logger import get_logger
from src.core.security import hash_password, verify_password
from src.models.setting import Setting

logger = get_logger("auth_service")

ADMIN_AUTH_LOCK_ID = 73541630
SETTING_ADMIN_USERNAME = "admin_username"
SETTING_ADMIN_PASSWORD_HASH = "admin_password_hash"

_process_lock = asyncio.Lock()


class AuthService:
    @classmethod
    async def init_admin_credentials(cls) -> None:
        """
        Concurrency-safe administrator credentials initializer and reset handler.
        Uses PostgreSQL session-level advisory lock (ID 73541630) on a dedicated connection.
        Precedence:
        - Existing DB + ADMIN_PASSWORD_RESET != "true" -> DB remains authoritative
        - Existing DB + ADMIN_PASSWORD_RESET == "true" -> use non-empty ADMIN_PASSWORD_HASH or hash(ADMIN_PASSWORD), else fail
        - No DB + non-empty ADMIN_PASSWORD_HASH -> initialize from supplied hash
        - No DB + non-empty ADMIN_PASSWORD -> hash with Argon2id and initialize
        - No DB + neither -> fail startup clearly
        """
        async with _process_lock:
            async with engine.connect() as conn:
                is_pg = conn.dialect.name == "postgresql"
                if is_pg:
                    # Dedicated session-level advisory lock
                    await conn.execute(text(f"SELECT pg_advisory_lock({ADMIN_AUTH_LOCK_ID})"))
                    await conn.rollback()  # clear autobegin transaction

                try:
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        # Re-read active settings from database while holding lock
                        stmt_user = select(Setting).where(
                            Setting.key == SETTING_ADMIN_USERNAME,
                            Setting.status == "ACTIVE",
                        )
                        stmt_hash = select(Setting).where(
                            Setting.key == SETTING_ADMIN_PASSWORD_HASH,
                            Setting.status == "ACTIVE",
                        )
                        user_row = (await session.execute(stmt_user)).scalar_one_or_none()
                        hash_row = (await session.execute(stmt_hash)).scalar_one_or_none()

                        reset_requested = os.getenv("ADMIN_PASSWORD_RESET", "false").strip().lower() == "true"

                        # Case 1: Existing DB credentials
                        if hash_row and hash_row.value:
                            if reset_requested:
                                # Must have non-empty new password or hash
                                new_hash: Optional[str] = None
                                if settings.ADMIN_PASSWORD_HASH and settings.ADMIN_PASSWORD_HASH.strip():
                                    new_hash = settings.ADMIN_PASSWORD_HASH.strip()
                                elif settings.ADMIN_PASSWORD and settings.ADMIN_PASSWORD.strip():
                                    new_hash = hash_password(settings.ADMIN_PASSWORD.strip())
                                else:
                                    raise RuntimeError(
                                        "Admin password reset requested via ADMIN_PASSWORD_RESET=true, "
                                        "but neither non-empty ADMIN_PASSWORD nor ADMIN_PASSWORD_HASH was provided."
                                    )

                                hash_row.value = new_hash
                                # Update username only if non-empty ADMIN_USERNAME is supplied
                                if settings.ADMIN_USERNAME and settings.ADMIN_USERNAME.strip():
                                    target_user = settings.ADMIN_USERNAME.strip()
                                    if user_row:
                                        user_row.value = target_user
                                    else:
                                        session.add(
                                            Setting(
                                                key=SETTING_ADMIN_USERNAME,
                                                status="ACTIVE",
                                                value=target_user,
                                                is_encrypted=False,
                                                description="Admin username",
                                            )
                                        )

                                await session.commit()
                                logger.info("Admin credentials successfully reset from environment.")
                            else:
                                logger.info("Database-stored admin credentials are authoritative.")
                            return

                        # Case 2: Fresh installation (No DB credentials)
                        init_hash: Optional[str] = None
                        if settings.ADMIN_PASSWORD_HASH and settings.ADMIN_PASSWORD_HASH.strip():
                            init_hash = settings.ADMIN_PASSWORD_HASH.strip()
                        elif settings.ADMIN_PASSWORD and settings.ADMIN_PASSWORD.strip():
                            init_hash = hash_password(settings.ADMIN_PASSWORD.strip())
                        else:
                            raise RuntimeError(
                                "Fresh installation requires an initial administrator credential. "
                                "Please define a non-empty ADMIN_PASSWORD or ADMIN_PASSWORD_HASH in the environment."
                            )

                        target_user = (settings.ADMIN_USERNAME or "admin").strip()
                        if not target_user:
                            target_user = "admin"

                        if user_row:
                            user_row.value = target_user
                        else:
                            session.add(
                                Setting(
                                    key=SETTING_ADMIN_USERNAME,
                                    status="ACTIVE",
                                    value=target_user,
                                    is_encrypted=False,
                                    description="Admin username",
                                )
                            )

                        session.add(
                            Setting(
                                key=SETTING_ADMIN_PASSWORD_HASH,
                                status="ACTIVE",
                                value=init_hash,
                                is_encrypted=False,
                                description="Admin password hash (Argon2id)",
                            )
                        )
                        await session.commit()
                        logger.info("Initial admin credentials successfully bootstrapped into database for user '%s'", target_user)

                finally:
                    if is_pg:
                        try:
                            await conn.execute(text(f"SELECT pg_advisory_unlock({ADMIN_AUTH_LOCK_ID})"))
                            await conn.rollback()
                        except Exception as e:
                            logger.warning("Failed to release admin advisory lock: %s", e)

    @classmethod
    async def authenticate_admin(
        cls,
        username: str,
        password: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        """
        Authenticate an administrator against PostgreSQL Setting table.
        Never compares plaintext passwords if hash exists.
        Never logs passwords or hashes.
        """
        if not username or not password:
            return False

        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(
                Setting.key.in_([SETTING_ADMIN_USERNAME, SETTING_ADMIN_PASSWORD_HASH]),
                Setting.status == "ACTIVE",
            )
            res = await sess.execute(stmt)
            items = {s.key: s.value for s in res.scalars().all()}
            active_username = items.get(SETTING_ADMIN_USERNAME) or (settings.ADMIN_USERNAME or "admin").strip()
            active_hash = items.get(SETTING_ADMIN_PASSWORD_HASH)

            if username != active_username:
                return False

            if active_hash:
                return verify_password(password, active_hash)

            return False
        finally:
            if own_session:
                await sess.close()
