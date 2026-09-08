import logging
import re
from datetime import datetime, timedelta
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

# Custom Discord Emoji Icons
class Icon:
    SETTINGS = "<:settings:1546231994151739533>"
    SECURITY = "<:security:1546231993136713762>"
    SEARCH = "<:search:1546231991932944537>"
    SAVE = "<:save:1546231990804553828>"
    PUBLIC = "<:public:1546231989709967371>"
    INFO = "<:info:1546231988640288889>"
    SCHEDULE = "<:schedule:1546549328552657007>"
    HOURGLASS = "<:schedule:1546549328552657007>"
    FORUM = "<:forum:1546231986379559024>"
    BULB = "<:bulb:1546231985305813024>"
    EDIT = "<:edit:1546231984223944876>"
    TRASH = "<:trash:1546231981489004644>"
    CHECK = "<:check:1546231979186585861>"
    CANCEL = "<:cancel:1546231977689219083>"
    AUTORENEW = "<:autorenew:1546231976669876385>"
    ARROW_RIGHT = "<:arrow_right:1546231975055204372>"
    ARROW_LEFT = "<:arrow_left:1546231973284937768>"
    ADD_NOTES = "<:add_notes:1546231972160999504>"
    ADD_BOX = "<:add_box:1546231971024343181>"
    ADD = "<:add:1546231969917050910>"
    CALENDAR_MONTH = "<:calendar_month_128dp_E3E3E3_FILL:1546545394677186631>"
    PENDING = "<:pending:1546545395943870615>"
    ACCOUNT = "<:account:1546545397110153226>"
    CAMPAIGN = "<:campaign:1546545398212993065>"
    LANGUAGE = "<:language:1546545399467085954>"
    FIRST_PAGE = "<:first_page:1546545401178488983>"
    LAST_PAGE = "<:last_page:1546545402575192084>"
    SEND = "<:send:1546545404265373727>"
    SHUFFLE = "<:shuffle:1546545405569933332>"
    DELETE_FOREVER = "<:delete_forever:1546545406983409815>"
    NOTIFICATIONS = "<:notifications:1546545410351435838>"
    TROPHY = "<:trophy:1546545411555201034>"
    HARD_DRIVE = "<:hard_drive:1546549321447641211>"
    SMART_TOY = "<:smart_toy:1546549324119412826>"
    BOT = "<:smart_toy:1546549324119412826>"
    SCHEDULE_PENDING = "<:schedule_pending:1546549326967345193>"
    # Server custom emojis
    QOTD = "<:qotd:1545794409352798298>"
    CALENDAR = "<:calendar_month_128dp_E3E3E3_FILL:1546545394677186631>"

# Storage & abuse safety limits
MAX_QUEUE_QUESTIONS = 500          # Max active questions waiting in queue per channel (~1.5 years of daily QOTD)
MAX_TOTAL_QUESTIONS = 2500         # Max total questions (queue + history) per server
MAX_PENDING_SUGGESTIONS = 100      # Max pending suggestions in server review mailbox
MAX_USER_PENDING_SUGGESTIONS = 3   # Max pending suggestions per individual user
MAX_QUESTION_LENGTH = 300          # Max character length for any single question
MAX_BATCH_ADD_QUESTIONS = 50       # Max questions allowed in a single bulk paste
MAX_FILE_UPLOAD_QUESTIONS = 500    # Max questions allowed in an uploaded .txt file

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
    "max_queue_limit",
})

VALID_SETTINGS_COLS = frozenset({
    "admin_channel_id", "language", "suggest_role_id",
})

logger = logging.getLogger("qotd")


def get_next_qotd_datetime(channel_row: dict, now: Optional[datetime] = None) -> datetime:
    """Calculates the exact next datetime when QOTD will be posted for a channel."""
    if now is None:
        now = datetime.now(QOTD_TIMEZONE)
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=QOTD_TIMEZONE)
        else:
            now = now.astimezone(QOTD_TIMEZONE)

    scheduled_raw = (channel_row.get("scheduled_time") or DEFAULT_SCHEDULED_TIME).strip()
    try:
        if ":" in scheduled_raw:
            parts = scheduled_raw.split(":")
            hour, minute = int(parts[0]), int(parts[1])
        else:
            hour, minute = int(scheduled_raw), 0
    except (ValueError, TypeError):
        hour, minute = DEFAULT_QOTD_HOUR, 0

    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    today_str = now.date().isoformat()
    last_posted = channel_row.get("last_posted_date")

    # If already posted today, or if target time today has passed, schedule for tomorrow
    if last_posted == today_str or now >= target_dt:
        target_dt += timedelta(days=1)

    return target_dt


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
