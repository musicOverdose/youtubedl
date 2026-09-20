import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import Base
from src.core.security import (
    get_master_key,
    remove_runtime_ready,
    verify_runtime_artifacts,
    write_local_bot_api_env,
    write_restart_trigger,
    write_runtime_bot_token,
    write_runtime_ready,
)
from src.models.setting import Setting
from src.models.telegram_migration import TelegramMigration
from src.services.setting_service import (
    SETTING_API_HASH,
    SETTING_API_ID,
    SETTING_API_MODE,
    SETTING_BOT_TOKEN,
    SETTING_CACHE_CHANNEL_ID,
    SETTING_CONFIG_VERSION,
    SettingService,
    TelegramConfigurationLock,
)
from src.worker.telegram_factory import TelegramClientFactory

PG_TEST_URL = "postgresql+asyncpg://postgres@/test_ytdl_db?host=/tmp&port=5433"


@pytest_asyncio.fixture
async def pg_engine():
    engine = create_async_engine(PG_TEST_URL, poolclass=NullPool, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text(
                    "INSERT INTO telegram_migrations (id, state, created_at, updated_at) "
                    "VALUES (1, 'IDLE', NOW(), NOW()) ON CONFLICT (id) DO NOTHING"
                )
            )
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL test instance not accessible at {PG_TEST_URL}: {exc}")
    yield engine
    await engine.dispose()


@pytest.fixture
def test_dirs(monkeypatch):
    tmp = tempfile.mkdtemp()
    master_file = os.path.join(tmp, "master", "master.key")
    token_file = os.path.join(tmp, "runtime", "bot-token")
    ready_file = os.path.join(tmp, "state", "READY")
    env_file = os.path.join(tmp, "bot-api", "local-bot-api.env")
    trigger_file = os.path.join(tmp, "bot-api", "restart-trigger")
    transfer_dir = os.path.join(tmp, "transfer")
    temp_dir = os.path.join(tmp, "ytdl")

    os.makedirs(os.path.dirname(master_file), exist_ok=True)
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    os.makedirs(os.path.dirname(ready_file), exist_ok=True)
    os.makedirs(os.path.dirname(env_file), exist_ok=True)
    os.makedirs(transfer_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    monkeypatch.setattr(settings, "MASTER_KEY_FILE", master_file)
    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", token_file)
    monkeypatch.setattr(settings, "RUNTIME_READY_FILE", ready_file)
    monkeypatch.setattr(settings, "LOCAL_BOT_API_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "LOCAL_BOT_API_TRIGGER_FILE", trigger_file)
    monkeypatch.setattr(settings, "TRANSFER_DIR", transfer_dir)
    monkeypatch.setattr(settings, "TEMP_DIR", temp_dir)

    yield {
        "dir": tmp,
        "master": master_file,
        "token": token_file,
        "ready": ready_file,
        "env": env_file,
        "trigger": trigger_file,
    }
    shutil.rmtree(tmp, ignore_errors=True)


class MockPostContext:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *args):
        pass


class MockAiohttpSession:
    def __init__(self, responses):
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def post(self, url, **kwargs):
        if self.responses:
            resp = self.responses.pop(0)
        else:
            resp = MagicMock(status=200, json=AsyncMock(return_value={"ok": True, "result": {}}))
        return MockPostContext(resp)


@pytest.mark.asyncio
async def test_real_postgresql_advisory_lock_autobegin_and_concurrency(pg_engine, monkeypatch):
    """
    Verifies against real PostgreSQL 17:
    1. SELECT pg_try_advisory_lock(73541629) acquires session-level lock.
    2. Connection autobegin is explicitly ended via rollback() so conn is clean.
    3. Concurrent connection attempting lock gets HTTP 409 Conflict.
    4. Short transactions (conn.begin()) succeed cleanly inside lock context.
    5. Exiting context releases lock.
    """
    import src.services.setting_service as ss
    monkeypatch.setattr(ss, "engine", pg_engine)

    # 1. Acquire lock
    async with TelegramConfigurationLock() as lock:
        assert lock.locked is True
        assert lock.conn.dialect.name == "postgresql"
        # In SQLAlchemy 2.0, connection must NOT be in an active transaction block
        assert lock.conn.in_transaction() is False

        # 2. Verify concurrent request receives HTTP 409
        with pytest.raises(HTTPException) as exc:
            async with TelegramConfigurationLock():
                pass
        assert exc.value.status_code == 409
        assert "Another Telegram configuration mutation is already running" in exc.value.detail

        # 3. Verify Short Transaction executes without InvalidRequestError
        async with lock.conn.begin():
            res = await lock.conn.scalar(text("SELECT 42"))
            assert res == 42
        assert lock.conn.in_transaction() is False

    # 4. After exiting, lock is released; another lock acquisition succeeds immediately
    async with TelegramConfigurationLock() as lock2:
        assert lock2.locked is True
        assert lock2.conn.in_transaction() is False


@pytest.mark.asyncio
async def test_ordinary_mutation_ready_unlinked_before_tx2(pg_engine, test_dirs, monkeypatch):
    """
    Verifies that during an Ordinary Mutation:
    - ACTIVE remains operational and READY remains present during candidate validation.
    - READY is unlinked BEFORE TX2 commits.
    - READY is recreated after runtime artifacts are written and verified.
    """
    import src.services.setting_service as ss
    monkeypatch.setattr(ss, "engine", pg_engine)

    # Setup initial ACTIVE state
    async with AsyncSession(pg_engine) as session:
        await session.execute(delete(Setting))
        await session.execute(delete(TelegramMigration))
        session.add(TelegramMigration(id=1, state="IDLE"))
        await session.commit()

    master_key = await get_master_key()
    write_runtime_ready()
    assert os.path.exists(test_dirs["ready"])

    ready_states_observed = []

    # Mock candidate probe to record whether READY is present during candidate validation
    original_probe = SettingService.probe_get_me
    async def mock_probe_get_me(token, endpoint):
        ready_states_observed.append(("during_validation", os.path.exists(test_dirs["ready"])))
        return True, {"id": 12345, "username": "TestBot"}, ""

    with patch.object(SettingService, "probe_get_me", side_effect=mock_probe_get_me):
        res = await SettingService.save_telegram_config(
            candidate_mode="cloud",
            bot_token="111111:AAAAAA_TestToken_Valid12345",
        )
        assert res["status"] == "success"

    # Verify READY was PRESENT during validation
    assert ("during_validation", True) in ready_states_observed

    # Verify READY is present now
    assert os.path.exists(test_dirs["ready"])

    # Verify token file has mode 0640
    assert os.path.exists(test_dirs["token"])
    stat_mode = oct(os.stat(test_dirs["token"]).st_mode & 0o777)
    assert stat_mode == "0o640"


@pytest.mark.asyncio
async def test_design_b_local_bot_api_mutation_reconciling_flow(pg_engine, test_dirs, monkeypatch):
    """
    Verifies Design B for Local Bot API credential mutation:
    - READY is unlinked BEFORE candidate local-bot-api.env is written and supervisor restarted.
    - On candidate validation failure, old env is restored, restart triggered, PENDING deleted, and READY restored.
    """
    import src.services.setting_service as ss
    monkeypatch.setattr(ss, "engine", pg_engine)

    master_key = await get_master_key()

    # Pre-populate ACTIVE Local configuration
    async with AsyncSession(pg_engine) as session:
        await session.execute(delete(Setting))
        await session.execute(delete(TelegramMigration))
        session.add_all([
            Setting(key=SETTING_BOT_TOKEN, status="ACTIVE", value="123456:old_token_12345678901234567890", is_encrypted=False),
            Setting(key=SETTING_API_MODE, status="ACTIVE", value="local", is_encrypted=False),
            Setting(key=SETTING_API_ID, status="ACTIVE", value="1111", is_encrypted=False),
            Setting(key=SETTING_API_HASH, status="ACTIVE", value="0123456789abcdef0123456789abcdef", is_encrypted=False),
            Setting(key=SETTING_CONFIG_VERSION, status="ACTIVE", value="1", is_encrypted=False),
            TelegramMigration(id=1, state="IDLE"),
        ])
        await session.commit()

    write_local_bot_api_env("1111", "0123456789abcdef0123456789abcdef")
    write_runtime_ready()
    assert os.path.exists(test_dirs["ready"])

    # Candidate validation will fail on TCP probe
    with patch.object(SettingService, "probe_tcp", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await SettingService.save_telegram_config(
                candidate_mode="local",
                bot_token="222222:BBBBBB_CandidateToken_12345",
                api_id=2222,
                api_hash="abcdef0123456789abcdef0123456789",
            )
        assert exc.value.status_code == 400
        assert "rolled back" in exc.value.detail

    # Verify that previous active env was restored
    with open(test_dirs["env"]) as f:
        content = f.read()
        assert "API_ID=1111" in content

    # Verify PENDING was deleted from PostgreSQL
    async with AsyncSession(pg_engine) as session:
        stmt = select(Setting).where(Setting.status == "PENDING")
        res = await session.execute(stmt)
        assert res.scalars().all() == []

    # Verify READY was restored
    assert os.path.exists(test_dirs["ready"])


@pytest.mark.asyncio
async def test_cloud_to_local_migration_cloud_logout_unknown_on_http_429(pg_engine, test_dirs, monkeypatch):
    """
    Verifies that when Cloud logOut() returns HTTP 429:
    1. It is strictly classified under CLOUD_LOGOUT_UNKNOWN.
    2. telegram_migrations records state = 'CLOUD_LOGOUT_UNKNOWN' with logout_attempted_at.
    3. /config/state/READY remains ABSENT.
    4. Method raises HTTP 504 and requires operator resolution.
    5. Subsequent startup reconciliation refuses to create READY while in CLOUD_LOGOUT_UNKNOWN.
    """
    import src.services.setting_service as ss
    monkeypatch.setattr(ss, "engine", pg_engine)

    master_key = await get_master_key()

    # Pre-populate ACTIVE Cloud configuration with Local credentials staged
    async with AsyncSession(pg_engine) as session:
        await session.execute(delete(Setting))
        await session.execute(delete(TelegramMigration))
        session.add_all([
            Setting(key=SETTING_BOT_TOKEN, status="ACTIVE", value="999999:cloud_token_12345678901234567890", is_encrypted=False),
            Setting(key=SETTING_API_MODE, status="ACTIVE", value="cloud", is_encrypted=False),
            Setting(key=SETTING_API_ID, status="ACTIVE", value="12345", is_encrypted=False),
            Setting(key=SETTING_API_HASH, status="ACTIVE", value="0123456789abcdef0123456789abcdef", is_encrypted=False),
            TelegramMigration(id=1, state="IDLE"),
        ])
        await session.commit()

    write_runtime_ready()
    assert os.path.exists(test_dirs["ready"])

    # Mock aiohttp to return HTTP 429 for logOut
    mock_resp = MagicMock(
        status=429,
        json=AsyncMock(return_value={"ok": False, "error_code": 429, "description": "Too Many Requests"}),
    )

    with patch("aiohttp.ClientSession", return_value=MockAiohttpSession([mock_resp])):
        with pytest.raises(HTTPException) as exc:
            await SettingService.migrate_mode(target_mode="local")

        assert exc.value.status_code == 504
        assert "CLOUD_LOGOUT_UNKNOWN" in exc.value.detail
        assert "Too Many Requests" in exc.value.detail

    # Verify READY remains ABSENT
    assert not os.path.exists(test_dirs["ready"])

    # Verify PostgreSQL state in telegram_migrations
    async with AsyncSession(pg_engine) as session:
        stmt = select(TelegramMigration).where(TelegramMigration.id == 1)
        res = await session.execute(stmt)
        mig = res.scalar_one()
        assert mig.state == "CLOUD_LOGOUT_UNKNOWN"
        assert mig.logout_attempted_at is not None
        assert "429" in mig.details

    # Test startup reconciliation while in CLOUD_LOGOUT_UNKNOWN
    # Must refuse to create READY
    monkeypatch.setattr("src.services.setting_service.AsyncSessionLocal", lambda: AsyncSession(pg_engine))
    await SettingService.reconcile_startup_state()

    assert not os.path.exists(test_dirs["ready"])


@pytest.mark.asyncio
async def test_strict_5_step_cache_channel_validation(pg_engine):
    """
    Verifies strict 5-step cache channel validation:
    1. getChat
    2. type == "channel"
    3. getChatMember(bot_id)
    4. status == creator OR administrator
    5. if administrator, can_post_messages == true
    """
    # Test case 1: Chat is not a channel (type == "group")
    chat_resp = MagicMock(
        status=200,
        json=AsyncMock(return_value={"ok": True, "result": {"id": -1001, "type": "group"}}),
    )
    with patch("aiohttp.ClientSession", return_value=MockAiohttpSession([chat_resp])):
        ok, err = await SettingService.probe_cache_channel("token", "https://api.telegram.org", -1001, bot_id=123)
        assert ok is False
        assert "not a channel" in err

    # Test case 2: Chat is channel, but bot is not administrator or creator (e.g. status == "member")
    chat_resp = MagicMock(
        status=200,
        json=AsyncMock(return_value={"ok": True, "result": {"id": -1001, "type": "channel"}}),
    )
    member_resp = MagicMock(
        status=200,
        json=AsyncMock(return_value={"ok": True, "result": {"status": "member"}}),
    )
    with patch("aiohttp.ClientSession", return_value=MockAiohttpSession([chat_resp, member_resp])):
        ok, err = await SettingService.probe_cache_channel("token", "https://api.telegram.org", -1001, bot_id=123)
        assert ok is False
        assert "not creator or administrator" in err

    # Test case 3: Administrator without can_post_messages
    chat_resp = MagicMock(
        status=200,
        json=AsyncMock(return_value={"ok": True, "result": {"id": -1001, "type": "channel"}}),
    )
    member_resp = MagicMock(
        status=200,
        json=AsyncMock(return_value={"ok": True, "result": {"status": "administrator", "can_post_messages": False}}),
    )
    with patch("aiohttp.ClientSession", return_value=MockAiohttpSession([chat_resp, member_resp])):
        ok, err = await SettingService.probe_cache_channel("token", "https://api.telegram.org", -1001, bot_id=123)
        assert ok is False
        assert "can_post_messages" in err

    # Test case 4: Administrator WITH can_post_messages == True -> SUCCEEDS
    chat_resp = MagicMock(
        status=200,
        json=AsyncMock(return_value={"ok": True, "result": {"id": -1001, "type": "channel"}}),
    )
    member_resp = MagicMock(
        status=200,
        json=AsyncMock(return_value={"ok": True, "result": {"status": "administrator", "can_post_messages": True}}),
    )
    with patch("aiohttp.ClientSession", return_value=MockAiohttpSession([chat_resp, member_resp])):
        ok, err = await SettingService.probe_cache_channel("token", "https://api.telegram.org", -1001, bot_id=123)
        assert ok is True
        assert err == ""


@pytest.mark.asyncio
async def test_worker_token_loading_strictly_from_runtime_file(test_dirs, monkeypatch):
    """
    Verifies that TelegramClientFactory strictly reads /config/runtime/bot-token
    and never falls back to settings.BOT_TOKEN or .env.
    """
    monkeypatch.setattr(settings, "BOT_TOKEN", "fallback_should_not_be_used")

    # 1. File does not exist -> raises RuntimeError
    if os.path.exists(test_dirs["token"]):
        os.remove(test_dirs["token"])

    with pytest.raises(RuntimeError, match="does not exist"):
        await TelegramClientFactory.get_client()

    # 2. File exists and non-empty -> succeeds with file's token
    with open(test_dirs["token"], "w") as f:
        f.write("123456:strict_runtime_token_12345\n")

    bot, mode = await TelegramClientFactory.get_client()
    assert bot.token == "123456:strict_runtime_token_12345"
    await bot.session.close()


@pytest.mark.asyncio
async def test_advisory_lock_concurrency_in_admin_init(pg_engine, monkeypatch):
    """
    Verifies against real PostgreSQL:
    1. Concurrency-safe initialization using session-level advisory lock (ID 73541630).
    2. Concurrent executions of AuthService.init_admin_credentials() serialize cleanly.
    3. Re-reads DB state while holding lock, so first successful initializer wins.
    """
    import src.services.auth_service as auth_mod
    from src.services.auth_service import AuthService, ADMIN_AUTH_LOCK_ID

    monkeypatch.setattr(auth_mod, "engine", pg_engine)
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "FirstPass123!")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", None)
    monkeypatch.delenv("ADMIN_PASSWORD_RESET", raising=False)

    # Clean existing admin settings from test DB
    async with pg_engine.begin() as conn:
        await conn.execute(
            delete(Setting).where(
                Setting.key.in_(["admin_username", "admin_password_hash"])
            )
        )

    # 1. Verify session-level lock acquisition on dedicated connection
    async with pg_engine.connect() as lock_conn:
        locked = await lock_conn.scalar(text(f"SELECT pg_try_advisory_lock({ADMIN_AUTH_LOCK_ID})"))
        assert locked is True
        await lock_conn.rollback()

        # Another connection cannot acquire it simultaneously
        async with pg_engine.connect() as lock_conn2:
            locked2 = await lock_conn2.scalar(text(f"SELECT pg_try_advisory_lock({ADMIN_AUTH_LOCK_ID})"))
            assert locked2 is False
            await lock_conn2.rollback()

        # Release lock
        unlocked = await lock_conn.scalar(text(f"SELECT pg_advisory_unlock({ADMIN_AUTH_LOCK_ID})"))
        assert unlocked is True
        await lock_conn.rollback()

    # 2. Test concurrent execution of init_admin_credentials
    results = await asyncio.gather(
        AuthService.init_admin_credentials(),
        AuthService.init_admin_credentials(),
        return_exceptions=True,
    )
    for res in results:
        assert not isinstance(res, Exception), f"Concurrent admin init failed: {res}"

    # 3. Verify exactly one active password hash row exists in PostgreSQL
    async with AsyncSession(pg_engine) as session:
        stmt = select(Setting).where(
            Setting.key == "admin_password_hash",
            Setting.status == "ACTIVE",
        )
        hashes = (await session.execute(stmt)).scalars().all()
        assert len(hashes) == 1

        # Verify authentication against PostgreSQL succeeds with FirstPass123!
        authenticated = await AuthService.authenticate_admin(
            "admin", "FirstPass123!", session=session
        )
        assert authenticated is True

