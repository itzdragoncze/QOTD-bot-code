import sys
from pathlib import Path
from typing import AsyncGenerator
import aiosqlite
import pytest
import pytest_asyncio

# Ensure root directory is on Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from translations import DEFAULT_LANGUAGE, DEFAULT_QUESTIONS


@pytest_asyncio.fixture
async def in_memory_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Creates a clean in-memory SQLite database initialized with the latest schema."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON;")

    # Settings table
    await conn.execute(
        """
        CREATE TABLE settings (
            guild_id INTEGER PRIMARY KEY,
            admin_channel_id INTEGER,
            language TEXT NOT NULL DEFAULT 'en',
            suggest_role_id INTEGER DEFAULT NULL
        );
        """
    )

    # qotd_channels table
    await conn.execute(
        """
        CREATE TABLE qotd_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL UNIQUE,
            role_id INTEGER DEFAULT NULL,
            scheduled_time TEXT NOT NULL DEFAULT '09:00',
            low_queue_threshold INTEGER NOT NULL DEFAULT 3,
            max_queue_limit INTEGER NOT NULL DEFAULT 500,
            last_posted_date TEXT DEFAULT NULL,
            last_thread_id INTEGER DEFAULT NULL,
            qotd_number INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    await conn.execute("CREATE INDEX idx_qotd_channels_guild ON qotd_channels(guild_id);")

    # questions table
    await conn.execute(
        """
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER DEFAULT NULL,
            question TEXT NOT NULL,
            normalized_text TEXT DEFAULT NULL,
            status TEXT NOT NULL CHECK(status IN ('to_ask', 'asked')),
            source TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL,
            asked_at TEXT,
            added_by_id INTEGER,
            added_by_name TEXT,
            suggested_by_id INTEGER,
            suggested_by_name TEXT,
            poll_options TEXT DEFAULT NULL
        );
        """
    )
    await conn.execute("CREATE INDEX idx_questions_guild_status ON questions(guild_id, status);")
    await conn.execute("CREATE INDEX idx_questions_channel_status ON questions(guild_id, channel_id, status);")
    await conn.execute("CREATE INDEX idx_questions_norm ON questions(guild_id, channel_id, normalized_text);")

    # suggestions table
    await conn.execute(
        """
        CREATE TABLE suggestions (
            id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            target_channel_id INTEGER DEFAULT NULL,
            question TEXT NOT NULL,
            normalized_text TEXT DEFAULT NULL,
            message TEXT NOT NULL DEFAULT '',
            avatar_url TEXT NOT NULL DEFAULT '',
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            review_message_id INTEGER DEFAULT NULL,
            admin_channel_id INTEGER DEFAULT NULL,
            poll_options TEXT DEFAULT NULL,
            language TEXT DEFAULT NULL
        );
        """
    )
    await conn.execute("CREATE INDEX idx_suggestions_guild ON suggestions(guild_id);")
    await conn.execute("CREATE INDEX idx_suggestions_target_channel ON suggestions(guild_id, target_channel_id);")
    await conn.execute("CREATE INDEX idx_suggestions_norm ON suggestions(guild_id, target_channel_id, normalized_text);")

    await conn.commit()

    yield conn

    await conn.close()
