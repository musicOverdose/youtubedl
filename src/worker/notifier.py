import asyncio
import time
from typing import Dict, List, Optional
from aiogram import Bot
from src.core.logger import setup_logger

logger = setup_logger("worker_notifier")


class StatusNotifier:
    def __init__(self, bot: Bot, requests: List[dict]):
        self.bot = bot
        self.requests = requests  # list of {"chat_id": int, "message_id": int}
        self.last_update_time: float = 0.0
        self.min_update_interval: float = 2.5  # Seconds between Telegram message edits

    async def update(
        self,
        stage_icon: str,
        stage_name: str,
        detail: str = "",
        force: bool = False,
    ) -> None:
        """
        Throttled edit of subscriber status messages.
        Avoids Telegram flood limits while keeping UX responsive.
        """
        now = time.time()
        if not force and (now - self.last_update_time < self.min_update_interval):
            return

        self.last_update_time = now

        text = f"{stage_icon} <b>{stage_name}</b>\n"
        if detail:
            text += f"{detail}\n"

        for req in self.requests:
            chat_id = req.get("chat_id")
            message_id = req.get("message_id")
            if not chat_id or not message_id:
                continue

            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                )
            except Exception:
                # Common when text hasn't changed or message was deleted
                pass
