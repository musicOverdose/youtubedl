import asyncio
import json
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
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
    remove_runtime_ready,
    verify_runtime_artifacts,
    write_local_bot_api_env,
    write_restart_trigger,
    write_runtime_bot_token,
    write_runtime_ready,
)
from src.models.setting import Setting
from src.models.telegram_migration import TelegramMigration

logger = get_logger("setting_service")

# Setting Keys
SETTING_BOT_TOKEN = "telegram_bot_token"
SETTING_API_ID = "telegram_api_id"
SETTING_API_HASH = "telegram_api_hash"
SETTING_API_MODE = "telegram_api_mode"
SETTING_CACHE_CHANNEL_ID = "telegram_cache_channel_id"
SETTING_CONFIG_VERSION = "telegram_config_version"
SETTING_MUST_JOIN_MSG = "must_join_message"
SETTING_WELCOME_MSG = "welcome_message"

DEFAULT_MUST_JOIN_MESSAGE = (
    "👋 Hello {first_name}!\n\n"
    "To use this bot, you must join our channel(s) first:\n"
    "{channel_list}\n\n"
    "After joining, please send your link again!"
)

DEFAULT_WELCOME_MESSAGE = (
    "👋 Hello, <b>{first_name}</b>!\n\n"
    "Send me any YouTube video or Shorts link, and I will download it for you in high quality.\n\n"
    "✨ <b>Features:</b>\n"
    "• Exact Video Resolutions (up to 4K)\n"
    "• 🎬 H.264 & 📦 H.265 / AAC options\n"
    "• 🎵 High-quality MP3 with ID3 cover art\n"
    "• 💬 Subtitles in 🇬🇧 English & 🇮🇷 Persian\n"
    "• Instant delivery for cached media"
)


class TelegramHTMLValidator(HTMLParser):
    ALLOWED_TAGS = {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "span", "tg-spoiler", "tg-emoji", "a", "code", "pre",
        "blockquote", "expandable_blockquote",
    }
    NO_ATTR_TAGS = {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "tg-spoiler", "blockquote", "expandable_blockquote",
    }

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.errors = []
        self.in_pre = False

    def handle_starttag(self, tag, attrs):
        lower_tag = tag.lower()
        if lower_tag not in self.ALLOWED_TAGS:
            self.errors.append(f"Tag <{tag}> is not supported by Telegram HTML.")
            return

        if self.in_pre:
            if lower_tag != "code":
                self.errors.append(f"Tag <{tag}> cannot be nested inside <pre>.")

        if lower_tag == "pre":
            if self.stack:
                self.errors.append("<pre> cannot be nested inside another tag.")
            self.in_pre = True

        attrs_dict = dict(attrs)

        if lower_tag in self.NO_ATTR_TAGS:
            if attrs:
                self.errors.append(f"Tag <{tag}> does not allow attributes.")
        elif lower_tag == "a":
            if "href" not in attrs_dict or not attrs_dict["href"].strip():
                self.errors.append("Tag <a> requires a non-empty 'href' attribute.")
            for attr_name in attrs_dict:
                if attr_name != "href":
                    self.errors.append(f"Tag <a> does not support attribute '{attr_name}'.")
        elif lower_tag == "span":
            if attrs_dict.get("class") != "tg-spoiler":
                self.errors.append("Tag <span> must have class=\"tg-spoiler\".")
            for attr_name in attrs_dict:
                if attr_name != "class":
                    self.errors.append(f"Tag <span> does not support attribute '{attr_name}'.")
        elif lower_tag == "tg-emoji":
            if "emoji-id" not in attrs_dict or not attrs_dict["emoji-id"].strip():
                self.errors.append("Tag <tg-emoji> requires a non-empty 'emoji-id' attribute.")
            for attr_name in attrs_dict:
                if attr_name != "emoji-id":
                    self.errors.append(f"Tag <tg-emoji> does not support attribute '{attr_name}'.")
        elif lower_tag in ("code", "pre"):
            for attr_name in attrs_dict:
                if attr_name != "class":
                    self.errors.append(f"Tag <{tag}> does not support attribute '{attr_name}'.")

        self.stack.append(lower_tag)

    def handle_endtag(self, tag):
        lower_tag = tag.lower()
        if lower_tag not in self.ALLOWED_TAGS:
            self.errors.append(f"Closing tag </{tag}> is not supported by Telegram HTML.")
            return

        if not self.stack:
            self.errors.append(f"Unmatched closing tag </{tag}>.")
            return

        top = self.stack.pop()
        if top != lower_tag:
            self.errors.append(f"Mismatched closing tag: expected </{top}>, got </{tag}>.")

        if lower_tag == "pre":
            self.in_pre = False


def validate_telegram_html(text: str) -> tuple[bool, str]:
    """
    Validate that text contains only Telegram-supported HTML tags,
    valid attributes, and balanced nesting.
    Returns (is_valid, error_message).
    """
    if not text or not text.strip():
        return False, "Message cannot be empty."

    # Temporarily substitute valid {first_name} placeholder before parsing
    test_text = text.replace("{first_name}", "User")
    validator = TelegramHTMLValidator()
    try:
        validator.feed(test_text)
        validator.close()
    except Exception as e:
        return False, f"Malformed HTML: {e}"

    if validator.errors:
        return False, "; ".join(validator.errors)

    if validator.stack:
        unclosed = ", ".join(f"<{t}>" for t in reversed(validator.stack))
        return False, f"Unclosed HTML tags: {unclosed}"

    return True, ""


class TelegramConfigurationLock:
    """
    Acquires PostgreSQL session-level advisory lock using SELECT pg_try_advisory_lock(73541629)
    on a dedicated connection. If the lock cannot be acquired immediately, raises HTTP 409 Conflict.
    The dedicated connection remains open until the context manager exits.
    Autobegin is explicitly ended via rollback() immediately after lock query to preserve
    clean transaction boundaries for subsequent Short Transactions.
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
            # End implicit autobegin transaction while preserving the session-level advisory lock
            await self.conn.rollback()
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
                        await self.conn.rollback()
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

    @staticmethod
    async def get_migration_state(session: AsyncSession) -> Tuple[str, Optional[datetime], Optional[str]]:
        """Retrieve the singleton migration state from telegram_migrations."""
        stmt = select(TelegramMigration).where(TelegramMigration.id == 1)
        res = await session.execute(stmt)
        row = res.scalar_one_or_none()
        if not row:
            row = TelegramMigration(id=1, state="IDLE")
            session.add(row)
            await session.flush()
        return row.state, row.logout_attempted_at, row.details

    @staticmethod
    async def set_migration_state(
        session: AsyncSession,
        state: str,
        logout_attempted_at: Optional[datetime] = None,
        details: Optional[str] = None,
    ) -> None:
        """Update the singleton migration state in telegram_migrations."""
        stmt = select(TelegramMigration).where(TelegramMigration.id == 1)
        res = await session.execute(stmt)
        row = res.scalar_one_or_none()
        if not row:
            row = TelegramMigration(
                id=1,
                state=state,
                logout_attempted_at=logout_attempted_at,
                details=details,
            )
            session.add(row)
        else:
            row.state = state
            row.logout_attempted_at = logout_attempted_at
            row.details = details
        await session.flush()

    # --------------------------------------------------------------------------
    # Telegram Configuration Read API
    # --------------------------------------------------------------------------

    @classmethod
    async def get_telegram_config(cls, session: Optional[AsyncSession] = None) -> Dict[str, Any]:
        """Return sanitized Telegram configuration for Web Admin."""
        all_active = await cls.get_all_active(session)
        bot_token = all_active.get(SETTING_BOT_TOKEN)
        api_id = all_active.get(SETTING_API_ID)
        api_hash = all_active.get(SETTING_API_HASH)
        mode = all_active.get(SETTING_API_MODE) or "local"
        cache_channel_id = all_active.get(SETTING_CACHE_CHANNEL_ID)
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

    @classmethod
    async def probe_cache_channel(
        cls,
        token: str,
        endpoint: str,
        channel_id: int,
        bot_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Strict 5-step cache channel validation:
        1. getChat(chat_id)
        2. verify result.type == "channel"
        3. getChatMember(chat_id, bot_id)
        4. verify status == creator OR administrator
        5. when administrator, require can_post_messages == true
        Do NOT use sendChatAction.
        """
        base_url = endpoint.rstrip('/')

        if bot_id is None:
            ok, bot_info, err = await cls.probe_get_me(token, endpoint)
            if not ok or not bot_info or "id" not in bot_info:
                return False, f"Cannot verify channel permissions without bot identity: {err}"
            bot_id = bot_info["id"]

        get_chat_url = f"{base_url}/bot{token}/getChat"
        get_member_url = f"{base_url}/bot{token}/getChatMember"

        try:
            async with aiohttp.ClientSession() as client:
                # 1. getChat
                async with client.post(get_chat_url, json={"chat_id": channel_id}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    chat_data = await resp.json()
                    if resp.status != 200 or not chat_data.get("ok"):
                        err = chat_data.get("description") or f"HTTP {resp.status}"
                        return False, f"getChat failed: {err}"
                    chat_result = chat_data.get("result", {})

                # 2. verify result.type == "channel"
                chat_type = chat_result.get("type")
                if chat_type != "channel":
                    return False, f"Chat is not a channel (type is '{chat_type}')"

                # 3. getChatMember(bot_id)
                async with client.post(get_member_url, json={"chat_id": channel_id, "user_id": bot_id}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    member_data = await resp.json()
                    if resp.status != 200 or not member_data.get("ok"):
                        err = member_data.get("description") or f"HTTP {resp.status}"
                        return False, f"getChatMember failed: {err}"
                    member_result = member_data.get("result", {})

                # 4. verify status == creator OR administrator
                status = member_result.get("status")
                if status not in ("creator", "administrator"):
                    return False, f"Bot is not creator or administrator of channel (status is '{status}')"

                # 5. when administrator, require can_post_messages == true
                if status == "administrator":
                    if not member_result.get("can_post_messages"):
                        return False, "Bot administrator does not have 'can_post_messages' permission in channel"

                return True, ""
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
        Distinguishes between Local Bot API mutations (Design B: RECONCILING state, unlink READY first)
        and Ordinary Mutations (ACTIVE remains operational during validation).
        """
        candidate_mode = candidate_mode.lower().strip()
        if candidate_mode not in ("local", "cloud"):
            raise HTTPException(status_code=400, detail="Mode must be either 'local' or 'cloud'.")

        async with TelegramConfigurationLock() as lock:
            conn = lock.conn
            master_key = await get_master_key()

            # Verify no migration workflow is active in PostgreSQL
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    mig_state, _, _ = await cls.get_migration_state(session)
                    if mig_state != "IDLE":
                        raise HTTPException(
                            status_code=409,
                            detail=f"Cannot change Telegram configuration: migration in progress (state: {mig_state}).",
                        )

                    stmt = select(Setting).where(Setting.status == "ACTIVE")
                    res = await session.execute(stmt)
                    active_items = {s.key: s for s in res.scalars().all()}

            existing_token = (
                (decrypt_credential(active_items[SETTING_BOT_TOKEN].value, master_key) if active_items[SETTING_BOT_TOKEN].is_encrypted else active_items[SETTING_BOT_TOKEN].value)
                if SETTING_BOT_TOKEN in active_items
                else None
            )
            existing_api_id = (
                active_items[SETTING_API_ID].value
                if SETTING_API_ID in active_items
                else None
            )
            existing_api_hash = (
                (decrypt_credential(active_items[SETTING_API_HASH].value, master_key) if active_items[SETTING_API_HASH].is_encrypted else active_items[SETTING_API_HASH].value)
                if SETTING_API_HASH in active_items
                else None
            )
            existing_channel_id = (
                active_items[SETTING_CACHE_CHANNEL_ID].value
                if SETTING_CACHE_CHANNEL_ID in active_items
                else None
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
                    await session.execute(delete(Setting).where(Setting.status == "PENDING"))

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

            # Determine whether this mutation alters the live Local Bot API runtime
            is_local_api_mutation = (
                candidate_mode == "local"
                and (
                    (target_api_id != existing_api_id)
                    or (target_api_hash != existing_api_hash)
                )
            )

            validation_passed = False
            validation_error = ""
            bot_info = None
            target_endpoint = cls.derive_endpoint(candidate_mode)

            # ------------------------------------------------------------------
            # Candidate Validation Execution
            # ------------------------------------------------------------------
            if is_local_api_mutation:
                # DESIGN B: Local Bot API runtime must be cycled to test candidate credentials.
                # Unlink READY FIRST to halt consumers while production runtime mutates.
                remove_runtime_ready()

                try:
                    write_local_bot_api_env(target_api_id, target_api_hash)
                    write_restart_trigger()
                    await asyncio.sleep(1.0)

                    # Probe TCP port
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

                    get_me_ok, bot_info, err = await cls.probe_get_me(target_token, target_endpoint)
                    if not get_me_ok:
                        raise ValueError(f"Local Bot API getMe probe failed: {err}")

                    if target_channel:
                        try:
                            cid = int(target_channel)
                            chan_ok, chan_err = await cls.probe_cache_channel(
                                target_token, target_endpoint, cid, bot_id=bot_info.get("id")
                            )
                            if not chan_ok:
                                raise ValueError(f"Cache channel validation failed: {chan_err}")
                        except ValueError as ve:
                            raise ValueError(f"Invalid cache channel: {ve}")

                    validation_passed = True

                except Exception as ex:
                    validation_passed = False
                    validation_error = str(ex)
                    logger.error("Local Bot API candidate validation failed: %s", validation_error)

                    # Rollback Local Bot API runtime to existing ACTIVE credentials
                    if existing_api_id and existing_api_hash:
                        try:
                            write_local_bot_api_env(existing_api_id, existing_api_hash)
                            write_restart_trigger()
                            await asyncio.sleep(1.0)
                        except Exception as r_err:
                            logger.error("Failed to restore previous local-bot-api.env: %s", r_err)

                    # Delete PENDING from PostgreSQL
                    async with conn.begin():
                        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                            await session.execute(delete(Setting).where(Setting.status == "PENDING"))

                    # Re-arm READY for the restored ACTIVE runtime if configured
                    if existing_token:
                        write_runtime_ready()

                    raise HTTPException(
                        status_code=400,
                        detail=f"Candidate Telegram configuration rejected: {validation_error}. System rolled back to previous configuration.",
                    )

            else:
                # ORDINARY MUTATION: Token rotation, cache channel ID, or Cloud mode.
                # Live runtime is NOT modified during candidate validation.
                # ACTIVE remains operational and READY remains present.
                try:
                    if candidate_mode == "local":
                        # Probe existing local server
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

                    get_me_ok, bot_info, err = await cls.probe_get_me(target_token, target_endpoint)
                    if not get_me_ok:
                        raise ValueError(f"Bot API getMe probe failed: {err}")

                    if target_channel:
                        try:
                            cid = int(target_channel)
                            chan_ok, chan_err = await cls.probe_cache_channel(
                                target_token, target_endpoint, cid, bot_id=bot_info.get("id")
                            )
                            if not chan_ok:
                                raise ValueError(f"Cache channel validation failed: {chan_err}")
                        except ValueError as ve:
                            raise ValueError(f"Invalid cache channel: {ve}")

                    validation_passed = True

                except Exception as ex:
                    validation_passed = False
                    validation_error = str(ex)
                    logger.error("Ordinary Telegram candidate validation failed: %s", validation_error)

                    # Delete PENDING from PostgreSQL
                    async with conn.begin():
                        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                            await session.execute(delete(Setting).where(Setting.status == "PENDING"))

                    raise HTTPException(
                        status_code=400,
                        detail=f"Candidate Telegram configuration rejected: {validation_error}. Active configuration remains unchanged.",
                    )

                # Validation succeeded for ordinary mutation: UNLINK READY FIRST before TX2
                remove_runtime_ready()

            # ------------------------------------------------------------------
            # Short Transaction 2: Promote PENDING to ACTIVE
            # ------------------------------------------------------------------
            new_version = 1
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    v_stmt = select(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
                    v_res = await session.execute(v_stmt)
                    v_item = v_res.scalar_one_or_none()
                    if v_item and v_item.value.isdigit():
                        new_version = int(v_item.value) + 1

                    p_stmt = select(Setting).where(Setting.status == "PENDING")
                    p_res = await session.execute(p_stmt)
                    pending_items = p_res.scalars().all()

                    keys_to_update = [item.key for item in pending_items]
                    if keys_to_update:
                        await session.execute(
                            delete(Setting).where(Setting.status == "ACTIVE", Setting.key.in_(keys_to_update))
                        )

                    await session.execute(
                        update(Setting).where(Setting.status == "PENDING").values(status="ACTIVE")
                    )

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

            # ------------------------------------------------------------------
            # Reconstruct Runtime Artifacts & Recreate READY
            # ------------------------------------------------------------------
            write_runtime_bot_token(target_token)
            if candidate_mode == "local" and target_api_id and target_api_hash:
                write_local_bot_api_env(target_api_id, target_api_hash)

            if not verify_runtime_artifacts(candidate_mode):
                logger.critical("Runtime artifacts failed disk verification after TX2 promotion!")
                raise RuntimeError("Runtime artifacts failed verification on disk. READY will NOT be created.")

            # Update Redis mirror
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

            # Atomically recreate READY
            write_runtime_ready()

            # Audit log
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
        1. Ensures directory permissions (2770 for runtime/bot-api, 0755 for transfer/tmp).
        2. Inspects telegram_migrations singleton workflow table.
        3. Discards stale PENDING records in PostgreSQL.
        4. Loads ACTIVE configuration strictly from PostgreSQL (NO .env fallback).
        5. Reconstructs /config/runtime/bot-token (0640, group ytdl-runtime GID 1001).
        6. Reconstructs /config/bot-api/local-bot-api.env (0640, group 101).
        7. Verifies runtime artifacts on disk.
        8. Updates Redis mirror (mode, config_version, cache_channel_id).
        9. Atomically creates /config/state/READY (0644).
        """
        logger.info("Starting startup state reconciliation...")
        try:
            ensure_directory(Path(settings.MASTER_KEY_FILE).parent, mode=0o700)
            ensure_directory(Path(settings.RUNTIME_BOT_TOKEN_FILE).parent, mode=0o2770, group="ytdl-runtime")
            ensure_directory(Path(settings.LOCAL_BOT_API_ENV_FILE).parent, mode=0o2770, group=101)
            ensure_directory(Path(settings.RUNTIME_READY_FILE).parent, mode=0o755)
            ensure_directory(settings.TRANSFER_DIR, mode=0o755)
            ensure_directory(settings.TEMP_DIR, mode=0o755)
        except Exception as e:
            logger.warning("Directory initialization warning: %s", e)

        async with AsyncSessionLocal() as session:
            # Check migration workflow state
            try:
                mig_state, logout_time, details = await cls.get_migration_state(session)
                if mig_state in ("CLOUD_LOGOUT_UNKNOWN", "LOCAL_VALIDATION_FAILED"):
                    logger.critical(
                        "Migration state is %s (attempted: %s, details: %s). "
                        "System halted; operator resolution required. READY will NOT be created.",
                        mig_state, logout_time, details
                    )
                    remove_runtime_ready()
                    return

                if mig_state == "CLOUD_LOGOUT_IN_PROGRESS":
                    logger.critical(
                        "Startup recovery: Found migration in CLOUD_LOGOUT_IN_PROGRESS. "
                        "Transitioning to CLOUD_LOGOUT_UNKNOWN. READY will NOT be created."
                    )
                    await cls.set_migration_state(
                        session,
                        "CLOUD_LOGOUT_UNKNOWN",
                        logout_attempted_at=datetime.now(timezone.utc),
                        details="Process restarted while CLOUD_LOGOUT_IN_PROGRESS",
                    )
                    await session.commit()
                    remove_runtime_ready()
                    return

                if mig_state == "CLOUD_LOGOUT_SUCCEEDED":
                    active_settings = await cls.get_all_active(session)
                    token = active_settings.get(SETTING_BOT_TOKEN)
                    if token:
                        probe_ok, _, err = await cls.probe_get_me(token, cls.derive_endpoint("local"))
                        if probe_ok:
                            await cls.set_migration_state(session, "IDLE")
                            await session.commit()
                        else:
                            await cls.set_migration_state(session, "LOCAL_VALIDATION_FAILED", details=err)
                            await session.commit()
                            remove_runtime_ready()
                            return
            except Exception as e:
                logger.error("Error checking migration state during startup: %s", e)

            # Discard stale PENDING records
            try:
                await session.execute(delete(Setting).where(Setting.status == "PENDING"))
                await session.commit()
            except Exception as e:
                logger.warning("Could not purge stale PENDING records: %s", e)
                await session.rollback()

            # Load ACTIVE configuration strictly from PostgreSQL
            try:
                active_settings = await cls.get_all_active(session)
            except Exception as e:
                logger.error("Failed to load ACTIVE settings: %s", e)
                active_settings = {}

        bot_token = active_settings.get(SETTING_BOT_TOKEN)
        api_mode = active_settings.get(SETTING_API_MODE) or "local"
        api_id = active_settings.get(SETTING_API_ID)
        api_hash = active_settings.get(SETTING_API_HASH)
        config_version = active_settings.get(SETTING_CONFIG_VERSION) or "1"
        cache_channel_id = active_settings.get(SETTING_CACHE_CHANNEL_ID)

        # If PostgreSQL has no ACTIVE bot token, do NOT synthesize from .env; keep READY absent
        if not bot_token:
            logger.warning(
                "No ACTIVE Telegram bot token in PostgreSQL database. "
                "Telegram service is unconfigured. /config/state/READY will NOT be created."
            )
            remove_runtime_ready()
            return

        # Reconstruct /config/runtime/bot-token (mode 0640, group ytdl-runtime GID 1001)
        try:
            write_runtime_bot_token(bot_token)
            logger.info("Reconstructed /config/runtime/bot-token (mode 0640, group ytdl-runtime)")
        except Exception as e:
            logger.error("Failed to write runtime bot token: %s", e)
            remove_runtime_ready()
            return

        # Reconstruct /config/bot-api/local-bot-api.env if local mode and credentials present
        if api_mode == "local" and api_id and api_hash:
            try:
                write_local_bot_api_env(api_id, api_hash)
                logger.info("Reconstructed /config/bot-api/local-bot-api.env (mode 0640, group 101)")
            except Exception as e:
                logger.error("Failed to write local-bot-api.env: %s", e)

        # Verify runtime artifacts on disk
        if not verify_runtime_artifacts(api_mode):
            logger.error("Runtime artifacts failed disk verification on startup. READY will NOT be created.")
            remove_runtime_ready()
            return

        # Populate Redis mirror
        try:
            r = get_redis_client()
            await r.set("telegram:active:mode", api_mode)
            await r.set("telegram:active:config_version", config_version)
            if cache_channel_id:
                await r.set("telegram:active:cache_channel_id", cache_channel_id)
        except Exception as e:
            logger.warning("Could not synchronize Redis cache during startup: %s", e)

        # Atomically create /config/state/READY (0644)
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
        Execute explicit mode migration between Cloud and Local Bot API.
        Enforces consumer halting before Cloud logOut(), uses telegram_migrations table,
        and classifies outcomes with strict CLOUD_LOGOUT_UNKNOWN handling.
        """
        target_mode = target_mode.lower().strip()
        if target_mode not in ("local", "cloud"):
            raise HTTPException(status_code=400, detail="Invalid target mode. Must be 'local' or 'cloud'.")

        async with TelegramConfigurationLock() as lock:
            conn = lock.conn
            master_key = await get_master_key()

            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    mig_state, logout_time, details = await cls.get_migration_state(session)
                    if mig_state not in ("IDLE", "LOCAL_VALIDATION_FAILED"):
                        raise HTTPException(
                            status_code=409,
                            detail=f"Migration already in state '{mig_state}'. Operator resolution required.",
                        )

                    stmt = select(Setting).where(Setting.status == "ACTIVE")
                    res = await session.execute(stmt)
                    items = {s.key: s for s in res.scalars().all()}

            current_mode = items.get(SETTING_API_MODE).value if SETTING_API_MODE in items else "local"
            bot_token = (
                (decrypt_credential(items[SETTING_BOT_TOKEN].value, master_key) if items[SETTING_BOT_TOKEN].is_encrypted else items[SETTING_BOT_TOKEN].value)
                if SETTING_BOT_TOKEN in items
                else None
            )
            if not bot_token:
                raise HTTPException(status_code=400, detail="Cannot migrate without an active Bot Token in PostgreSQL.")

            if current_mode == target_mode and target_mode == "local":
                # Local -> Local reload
                logger.info("Executing Local -> Local session reload...")
                write_restart_trigger()
                await asyncio.sleep(1.0)
                probe_ok, _, err = await cls.probe_get_me(bot_token, cls.derive_endpoint("local"))
                if not probe_ok:
                    raise HTTPException(status_code=500, detail=f"Local Bot API reload verification failed: {err}")
                return {"status": "success", "message": "Local Bot API reloaded successfully.", "mode": "local"}

            elif current_mode == target_mode:
                return {"status": "success", "message": f"Mode is already {target_mode}.", "mode": target_mode}

            elif current_mode == "cloud" and target_mode == "local":
                # Cloud -> Local migration
                api_id = items.get(SETTING_API_ID).value if SETTING_API_ID in items else None
                api_hash = (
                    (decrypt_credential(items[SETTING_API_HASH].value, master_key) if items[SETTING_API_HASH].is_encrypted else items[SETTING_API_HASH].value)
                    if SETTING_API_HASH in items
                    else None
                )
                if not api_id or not api_hash:
                    raise HTTPException(
                        status_code=400,
                        detail="Local Bot API credentials (API_ID, API_HASH) must be configured before migrating to local mode.",
                    )

                # 1. Stage migration intent & Local PENDING in DB
                async with conn.begin():
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        await cls.set_migration_state(session, "CLOUD_LOGOUT_IN_PROGRESS")
                        await session.execute(
                            delete(Setting).where(Setting.key == SETTING_API_MODE, Setting.status == "PENDING")
                        )
                        session.add(
                            Setting(
                                key=SETTING_API_MODE,
                                status="PENDING",
                                value="local",
                                is_encrypted=False,
                                description="Telegram Bot API Mode",
                            )
                        )
                        await session.flush()

                # 2. HALT CONSUMERS: remove READY FIRST
                remove_runtime_ready()

                # 3. Drain period
                await asyncio.sleep(2.0)

                # 4. Invoke Cloud logOut()
                cloud_logout_url = f"https://api.telegram.org/bot{bot_token}/logOut"
                logger.info("Calling Cloud Bot API logOut()...")

                logout_outcome = "UNKNOWN"
                error_detail = ""

                try:
                    async with aiohttp.ClientSession() as client:
                        async with client.post(cloud_logout_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            status_code = resp.status
                            try:
                                data = await resp.json()
                            except Exception:
                                data = {}

                            if status_code == 200 and data.get("ok") is True and data.get("result") is True:
                                logout_outcome = "CONFIRMED_SUCCESS"
                            elif status_code == 429:
                                logout_outcome = "UNKNOWN"
                                error_detail = f"Telegram HTTP 429 (Too Many Requests): {data.get('description', '')}"
                            elif status_code >= 500:
                                logout_outcome = "UNKNOWN"
                                error_detail = f"Telegram server error HTTP {status_code}: {data.get('description', '')}"
                            else:
                                logout_outcome = "UNKNOWN"
                                error_detail = f"Telegram HTTP {status_code}: {data.get('description', '')}"
                except asyncio.TimeoutError:
                    logout_outcome = "UNKNOWN"
                    error_detail = "Request timed out calling Cloud logOut"
                except (aiohttp.ClientError, OSError) as e:
                    logout_outcome = "UNKNOWN"
                    error_detail = f"Transport error calling Cloud logOut: {e}"

                # Branch based on outcome
                if logout_outcome == "CONFIRMED_SUCCESS":
                    async with conn.begin():
                        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                            await cls.set_migration_state(session, "CLOUD_LOGOUT_SUCCEEDED")

                    # Write local-bot-api.env & trigger reload
                    write_local_bot_api_env(api_id, api_hash)
                    write_restart_trigger()
                    await asyncio.sleep(1.0)

                    # Probe Local Bot API
                    probe_ok, bot_info, err = await cls.probe_get_me(bot_token, cls.derive_endpoint("local"))
                    if not probe_ok:
                        async with conn.begin():
                            async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                                await cls.set_migration_state(session, "LOCAL_VALIDATION_FAILED", details=err)
                        raise HTTPException(
                            status_code=502,
                            detail=f"Local Bot API verification failed after Cloud logOut: {err}. Cloud cooldown in effect; operator resolution required.",
                        )

                    # Local probe succeeded: Promote to ACTIVE
                    new_version = 1
                    async with conn.begin():
                        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                            v_stmt = select(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
                            v_res = await session.execute(v_stmt)
                            v_item = v_res.scalar_one_or_none()
                            if v_item and v_item.value.isdigit():
                                new_version = int(v_item.value) + 1

                            await session.execute(
                                update(Setting)
                                .where(Setting.key == SETTING_API_MODE, Setting.status == "ACTIVE")
                                .values(value="local")
                            )
                            await session.execute(
                                delete(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
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
                            await session.execute(delete(Setting).where(Setting.status == "PENDING"))
                            await cls.set_migration_state(session, "IDLE")

                    write_runtime_bot_token(bot_token)
                    verify_runtime_artifacts("local")

                    try:
                        r = get_redis_client()
                        await r.set("telegram:active:mode", "local")
                        await r.set("telegram:active:config_version", str(new_version))
                        await r.publish("telegram:config:reload", json.dumps({"mode": "local", "version": new_version}))
                    except Exception as re:
                        logger.warning("Redis sync error: %s", re)

                    write_runtime_ready()
                    return {"status": "success", "message": "Successfully migrated from Cloud to Local Bot API.", "mode": "local"}

                elif logout_outcome == "CONFIRMED_FAILURE":
                    async with conn.begin():
                        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                            await cls.set_migration_state(session, "IDLE")
                            await session.execute(delete(Setting).where(Setting.status == "PENDING"))
                    write_runtime_ready()
                    raise HTTPException(status_code=400, detail=f"Cloud logOut rejected before execution: {error_detail}")

                else:  # CLOUD_LOGOUT_UNKNOWN
                    async with conn.begin():
                        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                            await cls.set_migration_state(
                                session,
                                "CLOUD_LOGOUT_UNKNOWN",
                                logout_attempted_at=datetime.now(timezone.utc),
                                details=error_detail,
                            )
                    # KEEP READY ABSENT
                    raise HTTPException(
                        status_code=504,
                        detail=f"Cloud logOut outcome is CLOUD_LOGOUT_UNKNOWN ({error_detail}). Telegram may or may not have logged out. System held in halted state. Operator intervention required.",
                    )

            elif current_mode == "local" and target_mode == "cloud":
                # Local -> Cloud migration
                probe_ok, _, err = await cls.probe_get_me(bot_token, cls.derive_endpoint("cloud"))
                if not probe_ok:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cloud Bot API verification failed: {err}. Note that Telegram may enforce a 10-minute lockout after logOut.",
                    )

                remove_runtime_ready()
                new_version = 1
                async with conn.begin():
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        v_stmt = select(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
                        v_res = await session.execute(v_stmt)
                        v_item = v_res.scalar_one_or_none()
                        if v_item and v_item.value.isdigit():
                            new_version = int(v_item.value) + 1

                        await session.execute(
                            update(Setting)
                            .where(Setting.key == SETTING_API_MODE, Setting.status == "ACTIVE")
                            .values(value="cloud")
                        )
                        await session.execute(
                            delete(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
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
                        await cls.set_migration_state(session, "IDLE")

                write_runtime_bot_token(bot_token)
                verify_runtime_artifacts("cloud")

                try:
                    r = get_redis_client()
                    await r.set("telegram:active:mode", "cloud")
                    await r.set("telegram:active:config_version", str(new_version))
                    await r.publish("telegram:config:reload", json.dumps({"mode": "cloud", "version": new_version}))
                except Exception as re:
                    logger.warning("Redis sync error: %s", re)

                write_runtime_ready()
                return {"status": "success", "message": "Successfully migrated from Local to Cloud Bot API.", "mode": "cloud"}

            else:
                return {"status": "success", "message": f"Mode is already {target_mode}."}

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

    @classmethod
    async def get_welcome_message(cls, session: Optional[AsyncSession] = None) -> str:
        """Get the active welcome message template for /start."""
        custom_msg = await cls.get_active_setting(SETTING_WELCOME_MSG, session)
        return custom_msg if custom_msg else DEFAULT_WELCOME_MESSAGE

    @classmethod
    async def save_welcome_message(
        cls,
        message: str,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Validate and save a custom welcome message template for /start."""
        clean_msg = message.strip()
        is_valid, err = validate_telegram_html(clean_msg)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Telegram HTML formatting: {err}",
            )

        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.key == SETTING_WELCOME_MSG, Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            item = res.scalar_one_or_none()
            if item:
                item.value = clean_msg
            else:
                sess.add(
                    Setting(
                        key=SETTING_WELCOME_MSG,
                        status="ACTIVE",
                        value=clean_msg,
                        is_encrypted=False,
                        description="Custom Telegram Bot Welcome Message Template",
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
    async def reset_welcome_message(
        cls,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Reset the welcome message to system default."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            await sess.execute(
                delete(Setting).where(Setting.key == SETTING_WELCOME_MSG, Setting.status == "ACTIVE")
            )
            await sess.commit()
            return DEFAULT_WELCOME_MESSAGE
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

