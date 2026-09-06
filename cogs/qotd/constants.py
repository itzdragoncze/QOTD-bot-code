import logging
import re
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import discord

from translations import (
    DEFAULT_LANGUAGE,
    DEFAULT_QUESTIONS,
    SUPPORTED_LANGUAGES,
    t,
    resolve_user_locale,
)

DATABASE_FILE = "qotd.db"
LEGACY_SETTINGS_FILE = "qotd_settings.json"
DEFAULT_QOTD_HOUR = 9
DEFAULT_SCHEDULED_TIME = "09:00"
QOTD_TIMEZONE = ZoneInfo("Europe/Prague")
REVIEW_CUSTOM_ID = r"^qotd:review:(accept|decline|edit):(\d+):(.+)$"

# Storage & abuse safety limits
MAX_QUEUE_QUESTIONS = 500          # Max active questions waiting in queue per channel (~1.5 years of daily QOTD)
MAX_TOTAL_QUESTIONS = 2500         # Max total questions (queue + history) per server
MAX_PENDING_SUGGESTIONS = 100      # Max pending suggestions in server review mailbox
MAX_USER_PENDING_SUGGESTIONS = 3   # Max pending suggestions per individual user
MAX_QUESTION_LENGTH = 300          # Max character length for any single question
MAX_BATCH_ADD_QUESTIONS = 50       # Max questions allowed in a single bulk paste

# Brand colors for clean visual distinction
COLOR_QUEUE = discord.Colour(16760576)                  # Amber / Gold
COLOR_SUGGESTIONS = discord.Colour.from_str("#FEE75C")  # Yellow
COLOR_HISTORY = discord.Colour.from_str("#57F287")      # Green
COLOR_SETTINGS = discord.Colour.from_str("#9B59B6")     # Purple
COLOR_LEADERBOARD = discord.Colour.from_str("#F1C40F")  # Gold
COLOR_DETAIL = discord.Colour.from_str("#3498DB")       # Blue
COLOR_DANGER = discord.Colour.from_str("#ED4245")       # Red
COLOR_POST = discord.Colour.from_str("#387b44")         # Forest Green

VALID_CHANNEL_COLS = frozenset({
    "role_id", "scheduled_time", "low_queue_threshold",
    "last_posted_date", "last_thread_id", "qotd_number",
})

VALID_SETTINGS_COLS = frozenset({
    "admin_channel_id", "language",
})

logger = logging.getLogger("qotd")


def normalize_question(text: str) -> str:
    """Normalizes text for robust duplicate comparison (strips numbers, bullets, whitespace, case)."""
    cleaned = re.sub(r"^\s*(?:\d+[\.\)]|[-*•])\s*", "", text)
    cleaned = cleaned.strip().strip("\"'").lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def parse_question_input(text: str) -> List[str]:
    """Splits multiline input into individual questions, stripping empty lines and bullets."""
    lines = [line.strip() for line in text.splitlines()]
    questions = []
    for line in lines:
        if not line:
            continue
        cleaned = re.sub(r"^\s*(?:\d+[\.\)]|[-*•])\s*", "", line).strip()
        if cleaned:
            questions.append(cleaned)
    return questions


def format_discord_timestamp(iso_str: Optional[str], format_type: str = "f", lang: str = "en") -> str:
    if not iso_str:
        return t(lang, "unknown_date")
    try:
        dt = datetime.fromisoformat(iso_str)
        return f"<t:{int(dt.timestamp())}:{format_type}>"
    except (ValueError, TypeError):
        return iso_str[:10]


def is_admin(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )
