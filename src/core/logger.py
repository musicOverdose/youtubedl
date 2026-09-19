import logging
import re
import sys
from typing import Any


SECRET_PATTERNS = [
    # Telegram Bot Token: e.g. 123456789:ABCdef... or bot123456...
    re.compile(r"(bot)?\d+:[\w-]{20,}", re.IGNORECASE),
    # Telegram API Hash: 32 hex chars
    re.compile(r"(telegram_api_hash\s*[:=]\s*)['\"]?([a-f0-9]{32})['\"]?", re.IGNORECASE),
    # AI API key
    re.compile(r"(ai_api_key\s*[:=]\s*)['\"]?([a-zA-Z0-9_\-]{15,})['\"]?", re.IGNORECASE),
    # Bearer token
    re.compile(r"(bearer\s+)[a-zA-Z0-9_\-\.]{20,}", re.IGNORECASE),
    # Password
    re.compile(r"(password\s*[:=]\s*)['\"]?([^'\"\s]+)['\"]?", re.IGNORECASE),
]


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(self.redact(v) if isinstance(v, str) else v for v in record.args)
        return True

    @staticmethod
    def redact(text: str) -> str:
        # Redact bot tokens
        text = re.sub(r"(bot)?\d+:[\w-]{20,}", "[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
        # Redact password fields
        text = re.sub(r"(password\s*[:=]\s*)['\"][^'\"]+['\"]", r"\1'[REDACTED]'", text, flags=re.IGNORECASE)
        text = re.sub(r"(password\s*[:=]\s*)[^\s]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
        # Redact Bearer tokens
        text = re.sub(r"(bearer\s+)[a-zA-Z0-9_\-\.]{20,}", r"\1[REDACTED]", text, flags=re.IGNORECASE)
        return text


def setup_logger(service_name: str, log_level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(service_name)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        formatter = logging.Formatter(
            fmt='{"timestamp": "%(asctime)s", "service": "' + service_name + '", "level": "%(levelname)s", "message": "%(message)s"}',
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        handler.setFormatter(formatter)
        handler.addFilter(SensitiveDataFilter())
        logger.addHandler(handler)
        logger.propagate = False

    return logger
