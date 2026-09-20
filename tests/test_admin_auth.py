import pytest
from src.core.logger import SensitiveDataFilter
from src.core.security import (
    create_session_token,
    decode_session_token,
    hash_password,
    verify_password,
)


def test_argon2id_password_hashing():
    raw_pass = "SuperSecurePassword987!"
    hashed = hash_password(raw_pass)

    assert hashed.startswith("$argon2id$")
    assert verify_password(raw_pass, hashed) is True
    assert verify_password("WrongPassword", hashed) is False


def test_session_token_creation_and_decoding():
    token = create_session_token("admin", role="ADMIN")
    payload = decode_session_token(token)

    assert payload is not None
    assert payload.get("sub") == "admin"
    assert payload.get("role") == "ADMIN"
    assert "exp" in payload

    # Invalid token
    invalid_payload = decode_session_token("invalid.jwt.token")
    assert invalid_payload is None


def test_secret_redaction():
    filter_obj = SensitiveDataFilter()
    raw_log = "Connected to bot123456789:ABCdefGHIjklMNOpqrSTUvwxYZ with password='MySecretPassword!'"
    redacted = filter_obj.redact(raw_log)

    assert "123456789:ABC" not in redacted
    assert "MySecretPassword!" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_fresh_install_no_credentials_fails(monkeypatch):
    from src.core.config import settings
    from src.services.auth_service import AuthService

    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", None)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)

    with pytest.raises(RuntimeError) as exc:
        await AuthService.init_admin_credentials()
    assert "Fresh installation requires an initial administrator credential" in str(exc.value)


@pytest.mark.asyncio
async def test_fresh_install_with_password_and_auth(monkeypatch):
    from src.core.config import settings
    from src.services.auth_service import AuthService

    monkeypatch.setattr(settings, "ADMIN_USERNAME", "superadmin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "InitPassword123!")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", None)
    monkeypatch.delenv("ADMIN_PASSWORD_RESET", raising=False)

    await AuthService.init_admin_credentials()

    # Verify authentication succeeds with the initialized credentials
    assert await AuthService.authenticate_admin("superadmin", "InitPassword123!") is True
    # Verify wrong password / wrong username fail
    assert await AuthService.authenticate_admin("superadmin", "WrongPass") is False
    assert await AuthService.authenticate_admin("otheruser", "InitPassword123!") is False


@pytest.mark.asyncio
async def test_db_credentials_authoritative_over_env(monkeypatch):
    from src.core.config import settings
    from src.services.auth_service import AuthService

    # 1. Bootstrap initial credentials
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "OriginalPass123!")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", None)
    monkeypatch.delenv("ADMIN_PASSWORD_RESET", raising=False)

    await AuthService.init_admin_credentials()
    assert await AuthService.authenticate_admin("admin", "OriginalPass123!") is True

    # 2. Change environment password WITHOUT ADMIN_PASSWORD_RESET=true
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "NewEnvPassword456!")
    await AuthService.init_admin_credentials()

    # DB remains authoritative
    assert await AuthService.authenticate_admin("admin", "OriginalPass123!") is True
    assert await AuthService.authenticate_admin("admin", "NewEnvPassword456!") is False


@pytest.mark.asyncio
async def test_admin_password_reset_flag(monkeypatch):
    from src.core.config import settings
    from src.services.auth_service import AuthService

    # 1. Bootstrap initial
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "OldPass123!")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", None)
    monkeypatch.delenv("ADMIN_PASSWORD_RESET", raising=False)
    await AuthService.init_admin_credentials()

    # 2. Reset with ADMIN_PASSWORD_RESET=true
    monkeypatch.setenv("ADMIN_PASSWORD_RESET", "true")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "ResetPass789!")
    await AuthService.init_admin_credentials()

    # Old password fails, new reset password succeeds
    assert await AuthService.authenticate_admin("admin", "OldPass123!") is False
    assert await AuthService.authenticate_admin("admin", "ResetPass789!") is True


@pytest.mark.asyncio
async def test_advisory_lock_concurrency_in_admin_init(monkeypatch):
    import asyncio
    from src.core.config import settings
    from src.services.auth_service import AuthService

    monkeypatch.setattr(settings, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "ConcurrentPass123!")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", None)
    monkeypatch.delenv("ADMIN_PASSWORD_RESET", raising=False)

    # Concurrently execute init_admin_credentials
    results = await asyncio.gather(
        AuthService.init_admin_credentials(),
        AuthService.init_admin_credentials(),
        AuthService.init_admin_credentials(),
        return_exceptions=True,
    )
    for res in results:
        assert not isinstance(res, Exception), f"Concurrent init failed: {res}"

    assert await AuthService.authenticate_admin("admin", "ConcurrentPass123!") is True



