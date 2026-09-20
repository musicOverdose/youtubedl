import asyncio
import json
import re
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import aiohttp
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.core.logger import get_logger
from src.core.redis import get_redis_client
from src.core.security import (
    decrypt_credential,
    encrypt_credential,
    ensure_directory,
    get_master_key,
    mask_secret,
    write_local_bot_api_env,
    write_restart_trigger,
    write_runtime_bot_token,
    write_runtime_ready,
)
from src.models.setting import Setting

logger = get_logger("setting_service")

# Setting Keys
SETTING_BOT_TOKEN = "telegram_bot_token"
SETTING_API_ID = "telegram_api_id"
SETTING_API_HASH = "telegram_api_hash"
SETTING_API_MODE = "telegram_api_mode"
SETTING_CACHE_CHANNEL_ID = "telegram_cache_channel_id"
SETTING_CONFIG_VERSION = "telegram_config_version"
SETTING_MUST_JOIN_MSG = "must_join_message"

DEFAULT_MUST_JOIN_MESSAGE = (
    "👋 Hello {first_name}!\n\n"
    "To use this bot, you must join our channel(s) first:\n"
    "{channel_list}\n\n"
    "After joining, please send your link again!"
)


class TelegramConfigurationLock:
    """
    Acquires PostgreSQL session-level advisory lock using SELECT pg_try_advisory_lock(73541629)
    on a dedicated connection. If the lock cannot be acquired immediately, raises HTTP 409 Conflict.
    The dedicated connection remains open until the context manager exits.
    For non-PostgreSQL dialects (e.g. SQLite in test suite), an in-memory lock is simulated.
    """
    ADVISORY_LOCK_ID = 73541629
    _simulated_locks = set()

    def __init__(self):
        self.conn = None
        self.locked = False

    async def __aenter__(self):
        self.conn = await engine.connect()
        if self.conn.dialect.name == "postgresql":
            res = await self.conn.scalar(
                text(f"SELECT pg_try_advisory_lock({self.ADVISORY_LOCK_ID})")
            )
            self.locked = bool(res)
        else:
            # Simulated advisory lock for test environments
            if self.ADVISORY_LOCK_ID in TelegramConfigurationLock._simulated_locks:
                self.locked = False
            else:
                TelegramConfigurationLock._simulated_locks.add(self.ADVISORY_LOCK_ID)
                self.locked = True

        if not self.locked:
            await self.conn.close()
            self.conn = None
            raise HTTPException(
                status_code=409,
                detail="Another Telegram configuration mutation is already running. Please try again shortly.",
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            try:
                if self.locked:
                    if self.conn.dialect.name == "postgresql":
                        await self.conn.scalar(
                            text(f"SELECT pg_advisory_unlock({self.ADVISORY_LOCK_ID})")
                        )
                    else:
                        TelegramConfigurationLock._simulated_locks.discard(self.ADVISORY_LOCK_ID)
            except Exception as e:
                logger.warning("Error releasing advisory lock %s: %s", self.ADVISORY_LOCK_ID, e)
            finally:
                await self.conn.close()
                self.conn = None
                self.locked = False


class SettingService:
    @staticmethod
    def derive_endpoint(mode: str) -> str:
        if mode == "cloud":
            return "https://api.telegram.org"
        base = settings.TELEGRAM_API_BASE_URL.rstrip("/")
        if not base or "api.telegram.org" in base:
            return "http://telegram-bot-api:8081"
        return base

    # --------------------------------------------------------------------------
    # Database Settings Retrieval
    # --------------------------------------------------------------------------

    @staticmethod
    async def get_active_setting(key: str, session: Optional[AsyncSession] = None) -> Optional[str]:
        """Fetch active setting value, decrypting if encrypted."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.key == key, Setting.status == "ACTIVE")
            result = await sess.execute(stmt)
            item = result.scalar_one_or_none()
            if not item:
                return None
            if item.is_encrypted:
                master_key = await get_master_key(sess)
                return decrypt_credential(item.value, master_key)
            return item.value
        finally:
            if own_session:
                await sess.close()

    @staticmethod
    async def get_all_active(session: Optional[AsyncSession] = None) -> Dict[str, str]:
        """Fetch all ACTIVE settings as a dictionary with decrypted values."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.status == "ACTIVE")
            result = await sess.execute(stmt)
            items = result.scalars().all()
            res = {}
            master_key = None
            for item in items:
                if item.is_encrypted:
                    if master_key is None:
                        master_key = await get_master_key(sess)
                    res[item.key] = decrypt_credential(item.value, master_key)
                else:
                    res[item.key] = item.value
            return res
        finally:
            if own_session:
                await sess.close()

    # --------------------------------------------------------------------------
    # Telegram Configuration Read API
    # --------------------------------------------------------------------------

    @classmethod
    async def get_telegram_config(cls, session: Optional[AsyncSession] = None) -> Dict[str, Any]:
        """Return sanitized Telegram configuration for Web Admin."""
        all_active = await cls.get_all_active(session)
        bot_token = all_active.get(SETTING_BOT_TOKEN) or settings.BOT_TOKEN
        api_id = all_active.get(SETTING_API_ID)
        if api_id is None and settings.TELEGRAM_API_ID is not None:
            api_id = str(settings.TELEGRAM_API_ID)
        api_hash = all_active.get(SETTING_API_HASH) or settings.TELEGRAM_API_HASH
        mode = all_active.get(SETTING_API_MODE) or settings.TELEGRAM_API_MODE or "local"
        cache_channel_id = all_active.get(SETTING_CACHE_CHANNEL_ID)
        if cache_channel_id is None and settings.TELEGRAM_CACHE_CHANNEL_ID is not None:
            cache_channel_id = str(settings.TELEGRAM_CACHE_CHANNEL_ID)
        version_str = all_active.get(SETTING_CONFIG_VERSION) or "1"

        return {
            "mode": mode,
            "derived_endpoint": cls.derive_endpoint(mode),
            "is_configured": bool(bot_token),
            "bot_token_masked": mask_secret(bot_token),
            "has_bot_token": bool(bot_token),
            "api_id": int(api_id) if (api_id and api_id.isdigit()) else None,
            "api_hash_masked": mask_secret(api_hash),
            "has_api_hash": bool(api_hash),
            "cache_channel_id": int(cache_channel_id) if (cache_channel_id and (cache_channel_id.isdigit() or cache_channel_id.startswith("-"))) else None,
            "config_version": int(version_str) if version_str.isdigit() else 1,
        }

    # --------------------------------------------------------------------------
    # Probes and Validation
    # --------------------------------------------------------------------------

    @staticmethod
    async def probe_tcp(host: str, port: int, timeout_sec: float = 3.0) -> bool:
        """Verify TCP socket connectivity."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout_sec,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception as e:
            logger.debug("TCP probe to %s:%s failed: %s", host, port, e)
            return False

    @classmethod
    async def probe_get_me(cls, token: str, endpoint: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        Execute sanitized getMe probe against derived endpoint.
        Returns: (success, user_dict, error_message)
        """
        url = f"{endpoint.rstrip('/')}/bot{token}/getMe"
        sanitized_url = f"{endpoint.rstrip('/')}/bot<REDACTED>/getMe"
        logger.info("Executing getMe probe to %s", sanitized_url)

        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("ok"):
                        return True, data.get("result"), ""
                    err = data.get("description") or f"HTTP {resp.status}"
                    logger.warning("getMe probe to %s failed: %s", sanitized_url, err)
                    return False, None, err
        except asyncio.TimeoutError:
            return False, None, "Connection timed out"
        except Exception as e:
            logger.warning("getMe probe to %s encountered error: %s", sanitized_url, e)
            return False, None, str(e)

    @staticmethod
    async def probe_cache_channel(token: str, endpoint: str, channel_id: int) -> Tuple[bool, str]:
        """Verify bot can access the specified cache channel."""
        url = f"{endpoint.rstrip('/')}/bot{token}/getChat"
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(url, json={"chat_id": channel_id}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("ok"):
                        return True, ""
                    err = data.get("description") or f"HTTP {resp.status}"
                    return False, err
        except Exception as e:
            return False, str(e)

    # --------------------------------------------------------------------------
    # Staged Telegram Configuration Update with Non-Blocking Advisory Lock
    # --------------------------------------------------------------------------

    @classmethod
    async def save_telegram_config(
        cls,
        candidate_mode: str,
        bot_token: Optional[str] = None,
        api_id: Optional[Union[int, str]] = None,
        api_hash: Optional[str] = None,
        cache_channel_id: Optional[Union[int, str]] = None,
        admin_username: str = "admin",
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Staged, atomic Telegram configuration mutation protected by pg_try_advisory_lock.
        Concurrent requests receive immediate HTTP 409 Conflict.
        """
        # Validate candidate syntax
        candidate_mode = candidate_mode.lower().strip()
        if candidate_mode not in ("local", "cloud"):
            raise HTTPException(status_code=400, detail="Mode must be either 'local' or 'cloud'.")

        # 1. Acquire dedicated DB connection and session-level advisory lock
        async with TelegramConfigurationLock() as lock:
            conn = lock.conn
            master_key = await get_master_key()

            # Read existing active configuration from connection
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    stmt = select(Setting).where(Setting.status == "ACTIVE")
                    res = await session.execute(stmt)
                    active_items = {s.key: s for s in res.scalars().all()}

            existing_token = (
                decrypt_credential(active_items[SETTING_BOT_TOKEN].value, master_key)
                if SETTING_BOT_TOKEN in active_items
                else settings.BOT_TOKEN
            )
            existing_api_id = (
                active_items[SETTING_API_ID].value
                if SETTING_API_ID in active_items
                else (str(settings.TELEGRAM_API_ID) if settings.TELEGRAM_API_ID else None)
            )
            existing_api_hash = (
                decrypt_credential(active_items[SETTING_API_HASH].value, master_key)
                if SETTING_API_HASH in active_items
                else settings.TELEGRAM_API_HASH
            )
            existing_channel_id = (
                active_items[SETTING_CACHE_CHANNEL_ID].value
                if SETTING_CACHE_CHANNEL_ID in active_items
                else (str(settings.TELEGRAM_CACHE_CHANNEL_ID) if settings.TELEGRAM_CACHE_CHANNEL_ID else None)
            )

            # Resolve effective candidate values (retain existing if not provided)
            target_token = bot_token.strip() if bot_token and bot_token.strip() else existing_token
            if not target_token:
                raise HTTPException(status_code=400, detail="Bot API Token is required.")
            if not re.match(r"^\d+:[\w-]{20,}$", target_token):
                raise HTTPException(status_code=400, detail="Invalid Telegram Bot Token format.")

            target_api_id = str(api_id).strip() if api_id else existing_api_id
            target_api_hash = api_hash.strip() if api_hash and api_hash.strip() else existing_api_hash

            if candidate_mode == "local":
                if not target_api_id or not str(target_api_id).isdigit():
                    raise HTTPException(status_code=400, detail="Telegram API ID (numeric) is required in Local mode.")
                if not target_api_hash or not re.match(r"^[a-fA-F0-9]{32}$", target_api_hash):
                    raise HTTPException(status_code=400, detail="Telegram API Hash (32 hex characters) is required in Local mode.")

            target_channel = str(cache_channel_id).strip() if cache_channel_id is not None else existing_channel_id

            # ------------------------------------------------------------------
            # Short Transaction 1: Create or update PENDING records in PostgreSQL
            # ------------------------------------------------------------------
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    # Clean any prior stale PENDING records
                    await session.execute(delete(Setting).where(Setting.status == "PENDING"))

                    # Stage candidate settings
                    pending_records = [
                        Setting(
                            key=SETTING_BOT_TOKEN,
                            status="PENDING",
                            value=encrypt_credential(target_token, master_key),
                            is_encrypted=True,
                            description="Encrypted Telegram Bot Token",
                        ),
                        Setting(
                            key=SETTING_API_MODE,
                            status="PENDING",
                            value=candidate_mode,
                            is_encrypted=False,
                            description="Telegram Bot API Mode (local or cloud)",
                        ),
                    ]
                    if target_api_id:
                        pending_records.append(
                            Setting(
                                key=SETTING_API_ID,
                                status="PENDING",
                                value=str(target_api_id),
                                is_encrypted=False,
                                description="Telegram API ID",
                            )
                        )
                    if target_api_hash:
                        pending_records.append(
                            Setting(
                                key=SETTING_API_HASH,
                                status="PENDING",
                                value=encrypt_credential(target_api_hash, master_key),
                                is_encrypted=True,
                                description="Encrypted Telegram API Hash",
                            )
                        )
                    if target_channel:
                        pending_records.append(
                            Setting(
                                key=SETTING_CACHE_CHANNEL_ID,
                                status="PENDING",
                                value=str(target_channel),
                                is_encrypted=False,
                                description="Telegram Cache Channel ID",
                            )
                        )

                    session.add_all(pending_records)
                    await session.flush()
                    # Exiting conn.begin() commits Short Transaction 1

            # ------------------------------------------------------------------
            # External Validation & Probing (outside transaction)
            # ------------------------------------------------------------------
            backup_env_file: Optional[Path] = None
            env_file = Path(settings.LOCAL_BOT_API_ENV_FILE)
            if env_file.is_file():
                try:
                    backup_env_file = env_file.parent / f".backup_{env_file.name}_{asyncio.get_event_loop().time()}"
                    backup_env_file.write_text(env_file.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception as e:
                    logger.warning("Could not backup local-bot-api.env: %s", e)

            validation_passed = False
            validation_error = ""
            bot_info = None

            try:
                target_endpoint = cls.derive_endpoint(candidate_mode)

                if candidate_mode == "local":
                    # Write candidate env file and touch restart-trigger
                    write_local_bot_api_env(target_api_id, target_api_hash)
                    write_restart_trigger()

                    # Wait briefly for supervisor to cycle child process
                    await asyncio.sleep(1.0)

                    # Probe TCP port 8081
                    # Extract host & port from TELEGRAM_API_BASE_URL
                    probe_host = "telegram-bot-api"
                    probe_port = 8081
                    try:
                        base_clean = settings.TELEGRAM_API_BASE_URL.split("://")[-1]
                        parts = base_clean.split(":")
                        probe_host = parts[0]
                        if len(parts) > 1:
                            probe_port = int(parts[1].split("/")[0])
                    except Exception:
                        pass

                    tcp_ok = await cls.probe_tcp(probe_host, probe_port, timeout_sec=4.0)
                    if not tcp_ok:
                        raise ValueError(f"TCP connectivity probe to Local Bot API ({probe_host}:{probe_port}) failed.")

                    # Probe getMe
                    get_me_ok, bot_info, err = await cls.probe_get_me(target_token, target_endpoint)
                    if not get_me_ok:
                        raise ValueError(f"Local Bot API getMe probe failed: {err}")

                else:
                    # Cloud mode: probe directly against https://api.telegram.org
                    get_me_ok, bot_info, err = await cls.probe_get_me(target_token, target_endpoint)
                    if not get_me_ok:
                        raise ValueError(f"Cloud Bot API getMe probe failed: {err}")

                # If cache channel specified, verify bot has access
                if target_channel:
                    try:
                        cid = int(target_channel)
                        chan_ok, chan_err = await cls.probe_cache_channel(target_token, target_endpoint, cid)
                        if not chan_ok:
                            logger.warning("Cache channel probe failed: %s (non-fatal)", chan_err)
                    except ValueError:
                        pass

                validation_passed = True

            except Exception as ex:
                validation_passed = False
                validation_error = str(ex)
                logger.error("Telegram candidate validation failed: %s", validation_error)

            # ------------------------------------------------------------------
            # Rollback on Validation Failure
            # ------------------------------------------------------------------
            if not validation_passed:
                # Restore backup local-bot-api.env if present
                if backup_env_file and backup_env_file.is_file():
                    try:
                        env_file.write_text(backup_env_file.read_text(encoding="utf-8"), encoding="utf-8")
                        write_restart_trigger()
                        backup_env_file.unlink()
                    except Exception as e:
                        logger.error("Failed to restore backup local-bot-api.env: %s", e)

                # Delete PENDING from PostgreSQL
                async with conn.begin():
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        await session.execute(delete(Setting).where(Setting.status == "PENDING"))

                raise HTTPException(
                    status_code=400,
                    detail=f"Candidate Telegram configuration rejected: {validation_error}. System rolled back to previous configuration.",
                )

            # ------------------------------------------------------------------
            # Short Transaction 2: Promote PENDING to ACTIVE
            # ------------------------------------------------------------------
            new_version = 1
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    # Fetch current version
                    v_stmt = select(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
                    v_res = await session.execute(v_stmt)
                    v_item = v_res.scalar_one_or_none()
                    if v_item and v_item.value.isdigit():
                        new_version = int(v_item.value) + 1

                    # Fetch PENDING items
                    p_stmt = select(Setting).where(Setting.status == "PENDING")
                    p_res = await session.execute(p_stmt)
                    pending_items = p_res.scalars().all()

                    # Delete existing ACTIVE versions of these keys
                    keys_to_update = [item.key for item in pending_items]
                    if keys_to_update:
                        await session.execute(
                            delete(Setting).where(Setting.status == "ACTIVE", Setting.key.in_(keys_to_update))
                        )

                    # Promote PENDING to ACTIVE
                    await session.execute(
                        update(Setting).where(Setting.status == "PENDING").values(status="ACTIVE")
                    )

                    # Update or insert config version
                    await session.execute(
                        delete(Setting).where(Setting.status == "ACTIVE", Setting.key == SETTING_CONFIG_VERSION)
                    )
                    session.add(
                        Setting(
                            key=SETTING_CONFIG_VERSION,
                            status="ACTIVE",
                            value=str(new_version),
                            is_encrypted=False,
                            description="Telegram Configuration Version",
                        )
                    )
                    await session.flush()

            # Cleanup backup env file
            if backup_env_file and backup_env_file.is_file():
                try:
                    backup_env_file.unlink()
                except Exception:
                    pass

            # Update runtime bot-token file
            write_runtime_bot_token(target_token)

            # Publish updated state to Redis
            try:
                r = get_redis_client()
                await r.set("telegram:active:mode", candidate_mode)
                await r.set("telegram:active:config_version", str(new_version))
                if target_channel:
                    await r.set("telegram:active:cache_channel_id", str(target_channel))
                await r.publish(
                    "telegram:config:reload",
                    json.dumps({"version": new_version, "mode": candidate_mode}),
                )
            except Exception as e:
                logger.warning("Failed to publish config reload event to Redis: %s", e)

            # Log audit
            try:
                from src.services.audit_service import AuditService
                async with AsyncSessionLocal() as audit_sess:
                    await AuditService.log_action(
                        audit_sess,
                        "TELEGRAM_CONFIG_UPDATE",
                        admin_username,
                        f"Updated Telegram config to mode={candidate_mode}, version={new_version}",
                        ip_address=ip_address,
                    )
            except Exception as e:
                logger.warning("Could not write audit log: %s", e)

            return {
                "status": "success",
                "message": f"Telegram configuration successfully validated and activated in {candidate_mode} mode.",
                "mode": candidate_mode,
                "config_version": new_version,
                "bot_username": bot_info.get("username") if bot_info else None,
            }

    # --------------------------------------------------------------------------
    # Startup State Reconciliation
    # --------------------------------------------------------------------------

    @classmethod
    async def reconcile_startup_state(cls) -> None:
        """
        Runs on Web Admin startup:
        1. Ensures directory permissions (2770 for /config/runtime and /config/bot-api, 0755 for /transfer and /tmp/ytdl).
        2. Discards stale PENDING records in PostgreSQL.
        3. Loads ACTIVE configuration.
        4. Reconstructs /config/runtime/bot-token (0640, group ytdl-runtime).
        5. Reconstructs /config/bot-api/local-bot-api.env (0640, group 101).
        6. Updates Redis cache keys (mode, config_version).
        7. Atomically creates /config/state/READY (0644).
        """
        logger.info("Starting startup state reconciliation...")
        try:
            # Ensure directories based on configured paths
            ensure_directory(Path(settings.MASTER_KEY_FILE).parent, mode=0o700)
            ensure_directory(Path(settings.RUNTIME_BOT_TOKEN_FILE).parent, mode=0o2770, group="ytdl-runtime")
            ensure_directory(Path(settings.LOCAL_BOT_API_ENV_FILE).parent, mode=0o2770, group=101)
            ensure_directory(Path(settings.RUNTIME_READY_FILE).parent, mode=0o755)
            ensure_directory(settings.TRANSFER_DIR, mode=0o755)
            ensure_directory(settings.TEMP_DIR, mode=0o755)
        except Exception as e:
            logger.warning("Directory initialization warning: %s", e)

        async with AsyncSessionLocal() as session:
            # 1. Discard stale PENDING records
            try:
                await session.execute(delete(Setting).where(Setting.status == "PENDING"))
                await session.commit()
            except Exception as e:
                logger.warning("Could not purge stale PENDING records: %s", e)
                await session.rollback()

            # 2. Load ACTIVE configuration
            try:
                active_settings = await cls.get_all_active(session)
            except Exception as e:
                logger.error("Failed to load ACTIVE settings: %s", e)
                active_settings = {}

        bot_token = active_settings.get(SETTING_BOT_TOKEN) or settings.BOT_TOKEN
        api_mode = active_settings.get(SETTING_API_MODE) or settings.TELEGRAM_API_MODE or "local"
        api_id = active_settings.get(SETTING_API_ID) or (str(settings.TELEGRAM_API_ID) if settings.TELEGRAM_API_ID else None)
        api_hash = active_settings.get(SETTING_API_HASH) or settings.TELEGRAM_API_HASH
        config_version = active_settings.get(SETTING_CONFIG_VERSION) or "1"
        cache_channel_id = active_settings.get(SETTING_CACHE_CHANNEL_ID) or (str(settings.TELEGRAM_CACHE_CHANNEL_ID) if settings.TELEGRAM_CACHE_CHANNEL_ID else None)

        # 3. Reconstruct /config/runtime/bot-token if token available
        if bot_token:
            try:
                write_runtime_bot_token(bot_token)
                logger.info("Reconstructed /config/runtime/bot-token (mode 0640, group ytdl-runtime)")
            except Exception as e:
                logger.error("Failed to write runtime bot token: %s", e)

        # 4. Reconstruct /config/bot-api/local-bot-api.env if api_id/hash available
        if api_id and api_hash:
            try:
                write_local_bot_api_env(api_id, api_hash)
                logger.info("Reconstructed /config/bot-api/local-bot-api.env (mode 0640, group 101)")
            except Exception as e:
                logger.error("Failed to write local-bot-api.env: %s", e)

        # 5. Populate Redis cache
        try:
            r = get_redis_client()
            await r.set("telegram:active:mode", api_mode)
            await r.set("telegram:active:config_version", config_version)
            if cache_channel_id:
                await r.set("telegram:active:cache_channel_id", cache_channel_id)
        except Exception as e:
            logger.warning("Could not synchronize Redis cache during startup: %s", e)

        # 6. Atomically create /config/state/READY
        try:
            write_runtime_ready()
            logger.info("Created /config/state/READY (mode 0644). Startup state reconciled.")
        except Exception as e:
            logger.error("Failed to write /config/state/READY: %s", e)

    # --------------------------------------------------------------------------
    # Explicit Mode Migrations
    # --------------------------------------------------------------------------

    @classmethod
    async def migrate_mode(
        cls,
        target_mode: str,
        admin_username: str = "admin",
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute explicit mode migration (Cloud -> Local, Local -> Cloud, or Local -> Local reload).
        """
        target_mode = target_mode.lower().strip()
        if target_mode not in ("local", "cloud"):
            raise HTTPException(status_code=400, detail="Invalid target mode. Must be 'local' or 'cloud'.")

        async with TelegramConfigurationLock() as lock:
            conn = lock.conn
            master_key = await get_master_key()

            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    stmt = select(Setting).where(Setting.status == "ACTIVE")
                    res = await session.execute(stmt)
                    items = {s.key: s for s in res.scalars().all()}

            current_mode = items.get(SETTING_API_MODE).value if SETTING_API_MODE in items else "local"
            bot_token = (
                decrypt_credential(items[SETTING_BOT_TOKEN].value, master_key)
                if SETTING_BOT_TOKEN in items
                else settings.BOT_TOKEN
            )
            if not bot_token:
                raise HTTPException(status_code=400, detail="Cannot migrate without an active Bot Token.")

            if current_mode == target_mode and target_mode == "local":
                # Local -> Local reload
                logger.info("Executing Local -> Local session reload...")
                write_restart_trigger()
                await asyncio.sleep(1.0)
                probe_ok, _, err = await cls.probe_get_me(bot_token, cls.derive_endpoint("local"))
                if not probe_ok:
                    raise HTTPException(status_code=500, detail=f"Local Bot API reload verification failed: {err}")
                msg = "Local Bot API reloaded successfully."

            elif current_mode == "cloud" and target_mode == "local":
                # Cloud -> Local migration
                # 1. Log out from Cloud Bot API
                cloud_logout_url = f"https://api.telegram.org/bot{bot_token}/logOut"
                logger.info("Calling Cloud Bot API logOut()...")
                try:
                    async with aiohttp.ClientSession() as client:
                        async with client.post(cloud_logout_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            data = await resp.json()
                            if resp.status != 200 or not data.get("ok"):
                                logger.warning("Cloud logOut returned non-OK: %s", data)
                except Exception as e:
                    logger.warning("Error during Cloud Bot API logOut: %s", e)

                # 2. Touch restart-trigger and probe Local Bot API
                write_restart_trigger()
                await asyncio.sleep(1.0)
                probe_ok, _, err = await cls.probe_get_me(bot_token, cls.derive_endpoint("local"))
                if not probe_ok:
                    raise HTTPException(status_code=500, detail=f"Local Bot API verification failed after logOut: {err}")

                # 3. Update database ACTIVE mode
                async with conn.begin():
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        await session.execute(
                            update(Setting)
                            .where(Setting.key == SETTING_API_MODE, Setting.status == "ACTIVE")
                            .values(value="local")
                        )
                msg = "Successfully migrated from Cloud to Local Bot API."

            elif current_mode == "local" and target_mode == "cloud":
                # Local -> Cloud migration
                # Probe Cloud Bot API
                probe_ok, _, err = await cls.probe_get_me(bot_token, cls.derive_endpoint("cloud"))
                if not probe_ok:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cloud Bot API verification failed: {err}. Note that Telegram may enforce a 10-minute lockout after logOut.",
                    )

                # Update database ACTIVE mode
                async with conn.begin():
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        await session.execute(
                            update(Setting)
                            .where(Setting.key == SETTING_API_MODE, Setting.status == "ACTIVE")
                            .values(value="cloud")
                        )
                msg = "Successfully migrated from Local to Cloud Bot API."

            else:
                msg = f"Mode is already {target_mode}."

            # Update Redis and publish reload
            try:
                r = get_redis_client()
                await r.set("telegram:active:mode", target_mode)
                await r.publish("telegram:config:reload", json.dumps({"mode": target_mode}))
            except Exception as e:
                logger.warning("Failed to publish mode change to Redis: %s", e)

            return {"status": "success", "message": msg, "mode": target_mode}

    # --------------------------------------------------------------------------
    # Must-Join Customizable Message Management
    # --------------------------------------------------------------------------

    @classmethod
    async def get_must_join_message(cls, session: Optional[AsyncSession] = None) -> str:
        """Get the active Must-Join template message."""
        custom_msg = await cls.get_active_setting(SETTING_MUST_JOIN_MSG, session)
        return custom_msg if custom_msg else DEFAULT_MUST_JOIN_MESSAGE

    @classmethod
    async def save_must_join_message(
        cls,
        message: str,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Save a custom Must-Join message template."""
        clean_msg = message.strip()
        if not clean_msg:
            raise HTTPException(status_code=400, detail="Must-Join message cannot be empty.")

        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.key == SETTING_MUST_JOIN_MSG, Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            item = res.scalar_one_or_none()
            if item:
                item.value = clean_msg
            else:
                sess.add(
                    Setting(
                        key=SETTING_MUST_JOIN_MSG,
                        status="ACTIVE",
                        value=clean_msg,
                        is_encrypted=False,
                        description="Custom Must-Join Message Template",
                    )
                )
            await sess.commit()
            return clean_msg
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def reset_must_join_message(
        cls,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Reset the Must-Join message to system default."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            await sess.execute(
                delete(Setting).where(Setting.key == SETTING_MUST_JOIN_MSG, Setting.status == "ACTIVE")
            )
            await sess.commit()
            return DEFAULT_MUST_JOIN_MESSAGE
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()
