from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram Bot
    BOT_TOKEN: str = Field(default="", description="Telegram Bot API Token")
    TELEGRAM_API_ID: Optional[int] = Field(default=None, description="Telegram API ID")
    TELEGRAM_API_HASH: Optional[str] = Field(default=None, description="Telegram API Hash")
    TELEGRAM_API_MODE: str = Field(default="local", description="Telegram API Mode: local or cloud")
    TELEGRAM_API_BASE_URL: str = Field(
        default="http://telegram-bot-api:8081",
        description="Telegram Bot API Base URL (cloud or local bot API server)"
    )
    TELEGRAM_CACHE_CHANNEL_ID: Optional[int] = Field(
        default=None,
        description="Private Telegram channel ID used for media caching"
    )

    # Runtime File Paths & Segregated Volumes
    MASTER_KEY_FILE: str = Field(default="/config/master/master.key")
    RUNTIME_BOT_TOKEN_FILE: str = Field(default="/config/runtime/bot-token")
    RUNTIME_READY_FILE: str = Field(default="/config/state/READY")
    LOCAL_BOT_API_ENV_FILE: str = Field(default="/config/bot-api/local-bot-api.env")
    LOCAL_BOT_API_TRIGGER_FILE: str = Field(default="/config/bot-api/restart-trigger")
    TRANSFER_DIR: str = Field(default="/transfer")
    LOCAL_BOT_API_DATA_DIR: str = Field(default="/var/lib/telegram-bot-api")
    LOCAL_BOT_API_TEMP_DIR: str = Field(default="/tmp/telegram-bot-api")

    # Database & Redis
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://ytdl_user:ytdl_secure_pass@postgres:5432/ytdl_db"
    )
    REDIS_URL: str = Field(default="redis://redis:6379/0")

    # Web Admin
    WEB_PORT: int = Field(default=8080)
    ADMIN_USERNAME: str = Field(default="admin")
    ADMIN_PASSWORD: Optional[str] = Field(default=None)
    ADMIN_PASSWORD_HASH: Optional[str] = Field(default=None)
    SECRET_KEY: str = Field(default="dev_secret_key_please_change_in_production_32chars")

    # Resource & Queue Controls
    MAX_ACTIVE_JOBS: int = Field(default=1)
    MAX_CONCURRENT_PER_USER: int = Field(default=1)
    MAX_QUEUED_PER_USER: int = Field(default=5)
    MAX_TEMP_STORAGE_GB: int = Field(default=30)
    MAX_UPLOAD_SIZE_MB: int = Field(default=2000)

    # Duration Controls
    MAX_VIDEO_DURATION_SECONDS: int = Field(default=7200)
    ALLOW_UNKNOWN_DURATION: bool = Field(default=False)
    CACHE_HIT_BYPASSES_DURATION_LIMIT: bool = Field(default=True)
    DEFAULT_MAX_HEIGHT: int = Field(default=1080)
    PLAYLISTS_ENABLED: bool = Field(default=False)

    # Media Codecs & Features
    H264_ENABLED: bool = Field(default=True)
    H265_ENABLED: bool = Field(default=True)
    MP3_ENABLED: bool = Field(default=True)
    SUBTITLES_ENABLED: bool = Field(default=True)
    MUST_JOIN_ENABLED: bool = Field(default=False)
    MUST_JOIN_EXEMPT_USERS: str = Field(default="")

    # AI Translation
    AI_ENABLED: bool = Field(default=False)
    AI_PROVIDER: str = Field(default="openai")
    AI_API_KEY: Optional[str] = Field(default=None)
    AI_BASE_URL: str = Field(default="https://api.openai.com/v1")
    AI_MODEL: str = Field(default="gpt-4o-mini")
    AI_MAX_CHUNKS: int = Field(default=50)
    AI_CHUNK_SIZE: int = Field(default=10)
    AI_TIMEOUT: float = Field(default=120.0)

    # yt-dlp & Network
    YTDLP_PROXY: Optional[str] = Field(default=None)
    YTDLP_COOKIES_FILE: str = Field(default="/config/secrets/youtube-cookies.txt")
    YTDLP_COOKIES_ENABLED: bool = Field(default=False)

    # Logging
    LOG_LEVEL: str = Field(default="INFO")
    TEMP_DIR: str = Field(default="/tmp/ytdl")


settings = Settings()
