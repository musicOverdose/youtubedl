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
