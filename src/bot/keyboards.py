from typing import List, Optional
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from src.models.channel import RequiredChannel


def build_quality_keyboard(
    source_id: str,
    available_heights: List[int],
) -> InlineKeyboardMarkup:
    """
    STRICT QUALITY-FIRST MENU:
    Displays only actual source heights + MP3 + Subtitle.
    Does NOT show H.264 or H.265 at this stage!
    """
    keyboard: List[List[InlineKeyboardButton]] = []

    # Pair video resolutions two per row
    row: List[InlineKeyboardButton] = []
    for height in available_heights:
        btn = InlineKeyboardButton(
            text=f"{height}p",
            callback_data=f"q:{source_id}:{height}",
        )
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Bottom utility row
    bottom_row = [
        InlineKeyboardButton(text="🎵 MP3", callback_data=f"aud:{source_id}:MP3"),
        InlineKeyboardButton(text="💬 Subtitle", callback_data=f"sub_menu:{source_id}"),
    ]
    keyboard.append(bottom_row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_codec_keyboard(source_id: str, height: int) -> InlineKeyboardMarkup:
    """
    SECOND STEP: CODEC SELECTION
    Only shown AFTER the user picks a resolution.
    """
    keyboard = [
        [
            InlineKeyboardButton(
                text="🎬 H.264 / AAC",
                callback_data=f"c:{source_id}:H264:{height}",
            )
        ],
        [
            InlineKeyboardButton(
                text="📦 H.265 / AAC",
                callback_data=f"c:{source_id}:H265:{height}",
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data=f"back_q:{source_id}",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_subtitle_keyboard(
    source_id: str, has_english: bool, ai_available: bool
) -> InlineKeyboardMarkup:
    """
    SUBTITLE MENU:
    Only 🇬🇧 English and 🇮🇷 Persian exist.
    Persian appears ONLY when English exists AND AI is enabled and configured.
    """
    keyboard: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []

    if has_english:
        row.append(InlineKeyboardButton(text="🇬🇧 English", callback_data=f"sub:{source_id}:EN"))
        if ai_available:
            row.append(InlineKeyboardButton(text="🇮🇷 Persian", callback_data=f"sub:{source_id}:FA"))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(text="⬅️ Back", callback_data=f"back_q:{source_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_must_join_keyboard(
    channels: List[RequiredChannel], resume_action: Optional[str] = None
) -> InlineKeyboardMarkup:
    """
    MUST-JOIN ENFORCEMENT UI:
    Shows join links for required channels and authoritative Check Again button.
    """
    keyboard: List[List[InlineKeyboardButton]] = []

    for idx, ch in enumerate(channels, 1):
        # Determine join URL
        url = ch.invite_url
        if not url and ch.username:
            url = f"https://t.me/{ch.username.lstrip('@')}"

        text = f"📢 Join {ch.title}"
        if url:
            keyboard.append([InlineKeyboardButton(text=text, url=url)])
        else:
            keyboard.append([InlineKeyboardButton(text=f"📢 {ch.title} (ID: {ch.chat_id})", callback_data="none")])

    check_data = f"mj_chk:{resume_action}" if resume_action else "mj_chk"
    keyboard.append([
        InlineKeyboardButton(text="✅ I've Joined — Check Again", callback_data=check_data)
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_queue_status_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """
    Interactive buttons on active/queued jobs:
    [📊 Queue Status] [❌ Cancel]
    """
    keyboard = [
        [
            InlineKeyboardButton(text="📊 Queue Status", callback_data=f"q_stat:{job_id}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"q_cancel:{job_id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
