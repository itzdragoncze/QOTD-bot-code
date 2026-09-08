import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
import aiosqlite
import discord
from translations import t, DEFAULT_LANGUAGE, DEFAULT_QUESTIONS, SUPPORTED_LANGUAGES, resolve_user_locale
from .constants import (
    logger,
    DATABASE_FILE,
    DEFAULT_SCHEDULED_TIME,
    MAX_QUEUE_QUESTIONS,
    MAX_TOTAL_QUESTIONS,
    MAX_PENDING_SUGGESTIONS,
    MAX_USER_PENDING_SUGGESTIONS,
    MAX_QUESTION_LENGTH,
    MAX_BATCH_ADD_QUESTIONS,
    VALID_CHANNEL_COLS,
    VALID_SETTINGS_COLS,
    normalize_question,
    parse_question_input,
)
from .views import suggestion_review_view


class QotdDatabaseMixin:
    async def initialize_database(self):
        # Settings table: global preferences (guild_id, admin_channel_id, language)
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                role_id INTEGER,
                admin_channel_id INTEGER,
                hour_utc INTEGER NOT NULL DEFAULT 9,
                scheduled_time TEXT NOT NULL DEFAULT '09:00',
                qotd_number INTEGER NOT NULL DEFAULT 0,
                last_post_date TEXT,
                last_thread_id INTEGER DEFAULT NULL,
                low_queue_threshold INTEGER NOT NULL DEFAULT 3,
                language TEXT NOT NULL DEFAULT 'en'
            )
            """
        )

        # qotd_channels table: multi-channel decoupled scheduling & queues
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS qotd_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE,
                role_id INTEGER,
                scheduled_time TEXT DEFAULT '09:00',
                low_queue_threshold INTEGER DEFAULT 3,
                last_posted_date TEXT,
                last_thread_id INTEGER DEFAULT NULL,
                qotd_number INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_qotd_channels_guild ON qotd_channels(guild_id)")

        # questions table
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER DEFAULT NULL,
                question TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('to_ask', 'asked')),
                source TEXT NOT NULL DEFAULT 'default',
                created_at TEXT NOT NULL,
                asked_at TEXT,
                added_by_id INTEGER,
                added_by_name TEXT,
                suggested_by_id INTEGER,
                suggested_by_name TEXT,
                poll_options TEXT DEFAULT NULL
            )
            """
        )

        # suggestions table
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                target_channel_id INTEGER DEFAULT NULL,
                question TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                review_message_id INTEGER DEFAULT NULL,
                admin_channel_id INTEGER DEFAULT NULL,
                poll_options TEXT DEFAULT NULL,
                language TEXT DEFAULT NULL
            )
            """
        )

        # Column existence & migration checks
        async def get_cols(table: str) -> set:
            cur = await self.db.execute(f"PRAGMA table_info({table})")
            return {r[1] for r in await cur.fetchall()}

        q_cols = await get_cols("questions")
        if "channel_id" not in q_cols:
            await self.db.execute("ALTER TABLE questions ADD COLUMN channel_id INTEGER DEFAULT NULL")
        if "normalized_text" not in q_cols:
            await self.db.execute("ALTER TABLE questions ADD COLUMN normalized_text TEXT DEFAULT NULL")
            cur = await self.db.execute("SELECT id, question FROM questions WHERE normalized_text IS NULL")
            for r in await cur.fetchall():
                await self.db.execute("UPDATE questions SET normalized_text = ? WHERE id = ?", (normalize_question(r["question"]), r["id"]))

        sugg_cols = await get_cols("suggestions")
        if "target_channel_id" not in sugg_cols:
            await self.db.execute("ALTER TABLE suggestions ADD COLUMN target_channel_id INTEGER DEFAULT NULL")
        if "language" not in sugg_cols:
            await self.db.execute("ALTER TABLE suggestions ADD COLUMN language TEXT DEFAULT NULL")
        if "normalized_text" not in sugg_cols:
            await self.db.execute("ALTER TABLE suggestions ADD COLUMN normalized_text TEXT DEFAULT NULL")
            cur = await self.db.execute("SELECT id, question FROM suggestions WHERE normalized_text IS NULL")
            for r in await cur.fetchall():
                await self.db.execute("UPDATE suggestions SET normalized_text = ? WHERE id = ?", (normalize_question(r["question"]), r["id"]))

        chan_cols = await get_cols("qotd_channels")
        if "last_thread_id" not in chan_cols:
            await self.db.execute("ALTER TABLE qotd_channels ADD COLUMN last_thread_id INTEGER DEFAULT NULL")
        if "qotd_number" not in chan_cols:
            await self.db.execute("ALTER TABLE qotd_channels ADD COLUMN qotd_number INTEGER NOT NULL DEFAULT 0")
        if "max_queue_limit" not in chan_cols:
            await self.db.execute("ALTER TABLE qotd_channels ADD COLUMN max_queue_limit INTEGER NOT NULL DEFAULT 500")

        settings_cols = await get_cols("settings")
        if "language" not in settings_cols:
            await self.db.execute("ALTER TABLE settings ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")
        if "suggest_role_id" not in settings_cols:
            await self.db.execute("ALTER TABLE settings ADD COLUMN suggest_role_id INTEGER DEFAULT NULL")

        # Indexes
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_questions_guild_status ON questions(guild_id, status)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_questions_channel_status ON questions(guild_id, channel_id, status)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_questions_norm ON questions(guild_id, channel_id, normalized_text)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_questions_guild_source ON questions(guild_id, source, suggested_by_id)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_guild ON suggestions(guild_id)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_guild_user ON suggestions(guild_id, user_id)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_target_channel ON suggestions(guild_id, target_channel_id)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_norm ON suggestions(guild_id, target_channel_id, normalized_text)")

        # Migrate legacy single-channel settings into qotd_channels
        if "channel_id" in settings_cols:
            cursor = await self.db.execute(
                """
                SELECT guild_id, channel_id, role_id, scheduled_time, low_queue_threshold, last_post_date, qotd_number, last_thread_id
                FROM settings
                WHERE channel_id IS NOT NULL
                """
            )
            legacy_settings = await cursor.fetchall()
            now_iso = datetime.now(timezone.utc).isoformat()
            for r in legacy_settings:
                g_id = r[0]
                ch_id = r[1]
                r_id = r[2]
                s_time = r[3] or DEFAULT_SCHEDULED_TIME
                thresh = r[4] or 3
                last_date = r[5]
                q_num = r[6] or 0
                th_id = r[7]

                await self.db.execute(
                    """
                    INSERT OR IGNORE INTO qotd_channels (
                        guild_id, channel_id, role_id, scheduled_time, low_queue_threshold,
                        last_posted_date, last_thread_id, qotd_number, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (g_id, ch_id, r_id, s_time, thresh, last_date, th_id, q_num, now_iso),
                )
                await self.db.execute(
                    "UPDATE questions SET channel_id = ? WHERE guild_id = ? AND channel_id IS NULL",
                    (ch_id, g_id),
                )
                await self.db.execute(
                    "UPDATE suggestions SET target_channel_id = ? WHERE guild_id = ? AND target_channel_id IS NULL",
                    (ch_id, g_id),
                )

        # Load guild language cache
        cursor = await self.db.execute("SELECT guild_id, language FROM settings")
        for row in await cursor.fetchall():
            self.guild_languages[row["guild_id"]] = row["language"] or DEFAULT_LANGUAGE

        await self.db.commit()

    # -----------------------------------------------------------------------
    # Multi-Channel CRUD Operations
    # -----------------------------------------------------------------------

    async def get_qotd_channels(self, guild_id: int) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM qotd_channels WHERE guild_id = ? ORDER BY id ASC",
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_qotd_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM qotd_channels WHERE channel_id = ?",
            (channel_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_qotd_channel(
        self, guild_id: int, channel_input: Optional[Union[int, str, discord.abc.GuildChannel]]
    ) -> Optional[Dict[str, Any]]:
        """
        Resolves a user-provided channel input (ID, mention, name, or channel object)
        to a configured QOTD channel dict. Returns None if the channel does NOT have QOTD set up.
        """
        if channel_input is None:
            return None

        channels = await self.get_qotd_channels(guild_id)
        if not channels:
            return None

        if hasattr(channel_input, "id"):
            cid = channel_input.id
            for c in channels:
                if c["channel_id"] == cid:
                    return c
            return None

        raw = str(channel_input).strip()
        if not raw:
            return None

        # 1. Mention format: <#123456789>
        mention_match = re.fullmatch(r"<#(\d+)>", raw)
        if mention_match:
            cid = int(mention_match.group(1))
            for c in channels:
                if c["channel_id"] == cid:
                    return c
            return None

        # 2. Pure digit ID
        if raw.isdigit():
            cid = int(raw)
            for c in channels:
                if c["channel_id"] == cid:
                    return c

        # 3. Channel name matching (#name or name)
        clean_name = raw.lstrip("#").strip().lower()
        guild = self.bot.get_guild(guild_id)
        for c in channels:
            ch_obj = self.bot.get_channel(c["channel_id"]) or (guild.get_channel(c["channel_id"]) if guild else None)
            if ch_obj and ch_obj.name.lower() == clean_name:
                return c

        for c in channels:
            ch_obj = self.bot.get_channel(c["channel_id"]) or (guild.get_channel(c["channel_id"]) if guild else None)
            if ch_obj and (ch_obj.name == clean_name or f"#{ch_obj.name}" == raw):
                return c

        return None

    async def add_qotd_channel(
        self,
        guild_id: int,
        channel_id: int,
        role_id: Optional[int] = None,
        scheduled_time: str = DEFAULT_SCHEDULED_TIME,
        low_queue_threshold: int = 3,
        last_posted_date: Optional[str] = None,
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor = await self.db.execute(
            """
            INSERT INTO qotd_channels (
                guild_id, channel_id, role_id, scheduled_time, low_queue_threshold, created_at, last_posted_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, role_id, scheduled_time, low_queue_threshold, now_iso, last_posted_date),
        )
        await self.db.commit()

        # Seed starter questions specifically for this new channel
        server_lang = self.get_server_language(guild_id)
        await self.seed_default_questions(guild_id, channel_id=channel_id, lang=server_lang)
        return cursor.lastrowid

    _VALID_CHANNEL_COLS = frozenset({
        "role_id", "scheduled_time", "low_queue_threshold",
        "last_posted_date", "last_thread_id", "qotd_number",
        "max_queue_limit",
    })

    async def update_qotd_channel(self, channel_id: int, **kwargs):
        if not kwargs:
            return
        invalid = set(kwargs.keys()) - self._VALID_CHANNEL_COLS
        if invalid:
            raise ValueError(f"Invalid column names for qotd_channels: {invalid}")
        set_clause = ", ".join(f"{col} = ?" for col in kwargs)
        values = list(kwargs.values()) + [channel_id]
        await self.db.execute(f"UPDATE qotd_channels SET {set_clause} WHERE channel_id = ?", values)
        await self.db.commit()

    async def delete_qotd_channel(self, channel_id: int, delete_questions: bool = False):
        cursor = await self.db.execute("SELECT guild_id FROM qotd_channels WHERE channel_id = ?", (channel_id,))
        row = await cursor.fetchone()
        guild_id = row[0] if row else None

        await self.db.execute("DELETE FROM qotd_channels WHERE channel_id = ?", (channel_id,))
        if delete_questions:
            await self.db.execute("DELETE FROM questions WHERE channel_id = ?", (channel_id,))
            await self.db.execute("DELETE FROM suggestions WHERE target_channel_id = ?", (channel_id,))
            if guild_id:
                rem_cursor = await self.db.execute("SELECT COUNT(*) FROM qotd_channels WHERE guild_id = ?", (guild_id,))
                rem_row = await rem_cursor.fetchone()
                if rem_row and rem_row[0] == 0:
                    await self.db.execute("DELETE FROM questions WHERE guild_id = ? AND channel_id IS NULL", (guild_id,))
        await self.db.commit()

    # -----------------------------------------------------------------------
    # Guild Settings Operations
    # -----------------------------------------------------------------------

    async def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        cursor = await self.db.execute("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        if not row:
            return {
                "guild_id": guild_id,
                "admin_channel_id": None,
                "language": self.guild_languages.get(guild_id, DEFAULT_LANGUAGE),
                "suggest_role_id": None,
            }
        return dict(row)

    _VALID_SETTINGS_COLS = frozenset({
        "admin_channel_id", "language", "suggest_role_id",
    })

    async def update_guild_settings(self, guild_id: int, **kwargs):
        if not kwargs:
            return
        invalid = set(kwargs.keys()) - self._VALID_SETTINGS_COLS
        if invalid:
            raise ValueError(f"Invalid column names for settings: {invalid}")
        await self.db.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
        set_clause = ", ".join(f"{col} = ?" for col in kwargs)
        values = list(kwargs.values()) + [guild_id]
        await self.db.execute(f"UPDATE settings SET {set_clause} WHERE guild_id = ?", values)
        await self.db.commit()

    # -----------------------------------------------------------------------
    # Questions & Counts (Channel Scoped)
    # -----------------------------------------------------------------------

    async def get_queue_count(self, guild_id: int, channel_id: Optional[int] = None) -> int:
        if channel_id:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM questions WHERE guild_id = ? AND channel_id = ? AND status = 'to_ask'",
                (guild_id, channel_id),
            )
        else:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM questions WHERE guild_id = ? AND status = 'to_ask'",
                (guild_id,),
            )
        return (await cursor.fetchone())[0]

    async def get_total_question_count(self, guild_id: int, channel_id: Optional[int] = None) -> int:
        if channel_id:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM questions WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
        else:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM questions WHERE guild_id = ?",
                (guild_id,),
            )
        return (await cursor.fetchone())[0]

    async def get_pending_suggestions_count(self, guild_id: int, channel_id: Optional[int] = None) -> int:
        if channel_id:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM suggestions WHERE guild_id = ? AND target_channel_id = ?",
                (guild_id, channel_id),
            )
        else:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM suggestions WHERE guild_id = ?",
                (guild_id,),
            )
        return (await cursor.fetchone())[0]

    async def get_user_pending_suggestions_count(self, guild_id: int, user_id: int) -> int:
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM suggestions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return (await cursor.fetchone())[0]

    async def check_duplicates(
        self, guild_id: int, candidate_questions: List[str], channel_id: Optional[int] = None
    ) -> Tuple[List[str], List[str]]:
        candidate_norms = [normalize_question(q) for q in candidate_questions if normalize_question(q)]
        if not candidate_norms:
            return [], []

        placeholders = ",".join("?" for _ in candidate_norms)
        if channel_id:
            cursor = await self.db.execute(
                f"SELECT normalized_text FROM questions WHERE guild_id = ? AND channel_id = ? AND normalized_text IN ({placeholders})",
                (guild_id, channel_id, *candidate_norms),
            )
            existing_norms = {r[0] for r in await cursor.fetchall()}
            cursor_sugg = await self.db.execute(
                f"SELECT normalized_text FROM suggestions WHERE guild_id = ? AND target_channel_id = ? AND normalized_text IN ({placeholders})",
                (guild_id, channel_id, *candidate_norms),
            )
            existing_norms.update(r[0] for r in await cursor_sugg.fetchall())
        else:
            cursor = await self.db.execute(
                f"SELECT normalized_text FROM questions WHERE guild_id = ? AND normalized_text IN ({placeholders})",
                (guild_id, *candidate_norms),
            )
            existing_norms = {r[0] for r in await cursor.fetchall()}
            cursor_sugg = await self.db.execute(
                f"SELECT normalized_text FROM suggestions WHERE guild_id = ? AND normalized_text IN ({placeholders})",
                (guild_id, *candidate_norms),
            )
            existing_norms.update(r[0] for r in await cursor_sugg.fetchall())

        unique = []
        duplicates = []
        for q in candidate_questions:
            norm = normalize_question(q)
            if not norm:
                continue
            if norm in existing_norms:
                duplicates.append(q)
            else:
                existing_norms.add(norm)
                unique.append(q)
        return unique, duplicates

    async def add_question(
        self,
        guild_id: int,
        question: str,
        source: str = "manual",
        added_by: Optional[Union[discord.Member, discord.User]] = None,
        suggested_by_id: Optional[int] = None,
        suggested_by_name: Optional[str] = None,
        bypass_queue_limit: bool = False,
        channel_id: Optional[int] = None,
        bypass_total_limit: bool = False,
    ) -> Optional[int]:
        question_clean = question.strip()
        if not question_clean:
            return None
        if len(question_clean) > MAX_QUESTION_LENGTH:
            question_clean = question_clean[:MAX_QUESTION_LENGTH]

        if channel_id is None:
            channels = await self.get_qotd_channels(guild_id)
            if len(channels) == 1:
                channel_id = channels[0]["channel_id"]

        if not bypass_queue_limit:
            queue_count = await self.get_queue_count(guild_id, channel_id=channel_id)
            channel_max_limit = MAX_QUEUE_QUESTIONS
            if channel_id:
                ch_row = await self.get_qotd_channel(channel_id)
                if ch_row and ch_row.get("max_queue_limit") is not None:
                    channel_max_limit = ch_row["max_queue_limit"]
            if queue_count >= channel_max_limit:
                logger.warning("Queue limit reached for guild %s channel %s (%d/%d)", guild_id, channel_id, queue_count, channel_max_limit)
                return None

        if not bypass_total_limit:
            total_count = await self.get_total_question_count(guild_id)
            if total_count >= MAX_TOTAL_QUESTIONS:
                logger.warning("Total questions limit reached for guild %s (%d/%d)", guild_id, total_count, MAX_TOTAL_QUESTIONS)
                return None

        added_by_name = (getattr(added_by, "display_name", None) or getattr(added_by, "name", None) or str(added_by)) if added_by else None
        if added_by_name is not None and not isinstance(added_by_name, str):
            added_by_name = str(added_by_name)
        user_id = None
        if added_by and hasattr(added_by, "id"):
            try:
                user_id = int(added_by.id)
            except (ValueError, TypeError):
                user_id = None

        cursor = await self.db.execute(
            """
            INSERT INTO questions (
                guild_id, channel_id, question, normalized_text, status, source, created_at,
                added_by_id, added_by_name, suggested_by_id, suggested_by_name
            ) VALUES (?, ?, ?, ?, 'to_ask', ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                question_clean,
                normalize_question(question_clean),
                source,
                datetime.now(timezone.utc).isoformat(),
                user_id,
                added_by_name,
                suggested_by_id,
                suggested_by_name,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def add_multiple_questions(
        self,
        guild_id: int,
        questions: List[str],
        source: str = "manual",
        added_by: Optional[Union[discord.Member, discord.User]] = None,
        channel_id: Optional[int] = None,
    ) -> Tuple[List[int], int, Optional[str]]:
        """
        Atomically adds multiple questions to queue in a single transaction,
        strictly respecting per-channel queue limit and server total question limit.
        Returns (added_qids, skipped_capacity_count, limit_reason).
        """
        cleaned = [q.strip()[:MAX_QUESTION_LENGTH] for q in questions if q.strip()]
        if not cleaned:
            return [], 0, None

        if channel_id is None:
            channels = await self.get_qotd_channels(guild_id)
            if len(channels) == 1:
                channel_id = channels[0]["channel_id"]

        channel_max_limit = MAX_QUEUE_QUESTIONS
        if channel_id:
            ch_row = await self.get_qotd_channel(channel_id)
            if ch_row and ch_row.get("max_queue_limit") is not None:
                channel_max_limit = ch_row["max_queue_limit"]

        queue_count = await self.get_queue_count(guild_id, channel_id=channel_id)
        total_count = await self.get_total_question_count(guild_id)

        avail_queue = max(0, channel_max_limit - queue_count)
        avail_total = max(0, MAX_TOTAL_QUESTIONS - total_count)
        avail_slots = min(avail_queue, avail_total)

        if avail_slots <= 0:
            limit_reason = "total_limit" if avail_total <= 0 else "queue_full"
            return [], len(cleaned), limit_reason

        to_insert = cleaned[:avail_slots]
        skipped_capacity = len(cleaned) - len(to_insert)
        limit_reason = None
        if skipped_capacity > 0:
            limit_reason = "total_limit" if avail_total < avail_queue else "queue_full"

        added_by_name = (getattr(added_by, "display_name", None) or getattr(added_by, "name", None) or str(added_by)) if added_by else None
        if added_by_name is not None and not isinstance(added_by_name, str):
            added_by_name = str(added_by_name)
        now_str = datetime.now(timezone.utc).isoformat()
        user_id = None
        if added_by and hasattr(added_by, "id"):
            try:
                user_id = int(added_by.id)
            except (ValueError, TypeError):
                user_id = None

        added_qids: List[int] = []
        for q in to_insert:
            cursor = await self.db.execute(
                """
                INSERT INTO questions (
                    guild_id, channel_id, question, normalized_text, status, source, created_at,
                    added_by_id, added_by_name
                ) VALUES (?, ?, ?, ?, 'to_ask', ?, ?, ?, ?)
                """,
                (guild_id, channel_id, q, normalize_question(q), source, now_str, user_id, added_by_name),
            )
            if cursor.lastrowid:
                added_qids.append(cursor.lastrowid)

        await self.db.commit()
        return added_qids, skipped_capacity, limit_reason

    async def get_question(self, guild_id: int, question_id: int) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute("SELECT * FROM questions WHERE guild_id = ? AND id = ?", (guild_id, question_id))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def mark_question_asked(self, question_id: int):
        await self.db.execute(
            "UPDATE questions SET status = 'asked', asked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), question_id),
        )
        await self.db.commit()

    async def requeue_question(self, guild_id: int, question_id: int, channel_id: Optional[int] = None) -> bool:
        q_row = await self.get_question(guild_id, question_id)
        if not q_row:
            return False

        effective_ch = channel_id or q_row.get("channel_id")
        if effective_ch is None:
            channels = await self.get_qotd_channels(guild_id)
            if len(channels) == 1:
                effective_ch = channels[0]["channel_id"]

        cur_queue = await self.get_queue_count(guild_id, channel_id=effective_ch)
        channel_max_limit = MAX_QUEUE_QUESTIONS
        if effective_ch:
            ch_row = await self.get_qotd_channel(effective_ch)
            if ch_row and ch_row.get("max_queue_limit") is not None:
                channel_max_limit = ch_row["max_queue_limit"]
        if cur_queue >= channel_max_limit:
            return False

        result = await self.db.execute(
            "UPDATE questions SET status = 'to_ask', asked_at = NULL, channel_id = COALESCE(?, channel_id) WHERE guild_id = ? AND id = ?",
            (effective_ch, guild_id, question_id),
        )
        await self.db.commit()
        return result.rowcount > 0

    async def delete_single_question(self, guild_id: int, question_id: int) -> bool:
        result = await self.db.execute("DELETE FROM questions WHERE guild_id = ? AND id = ?", (guild_id, question_id))
        await self.db.commit()
        return result.rowcount > 0

    async def delete_multiple_questions(self, guild_id: int, question_ids: List[int], channel_id: Optional[int] = None) -> int:
        if not question_ids:
            return 0
        placeholders = ",".join("?" for _ in question_ids)
        if channel_id:
            result = await self.db.execute(
                f"DELETE FROM questions WHERE guild_id = ? AND channel_id = ? AND id IN ({placeholders})",
                [guild_id, channel_id, *question_ids],
            )
        else:
            result = await self.db.execute(
                f"DELETE FROM questions WHERE guild_id = ? AND id IN ({placeholders})",
                [guild_id, *question_ids],
            )
        await self.db.commit()
        return result.rowcount

    async def clear_questions(self, guild_id: int, status: str, channel_id: Optional[int] = None) -> int:
        if status == "suggestions":
            if channel_id:
                result = await self.db.execute("DELETE FROM suggestions WHERE guild_id = ? AND target_channel_id = ?", (guild_id, channel_id))
            else:
                result = await self.db.execute("DELETE FROM suggestions WHERE guild_id = ?", (guild_id,))
        elif status in ("asked", "to_ask"):
            if channel_id:
                result = await self.db.execute("DELETE FROM questions WHERE guild_id = ? AND channel_id = ? AND status = ?", (guild_id, channel_id, status))
            else:
                result = await self.db.execute("DELETE FROM questions WHERE guild_id = ? AND status = ?", (guild_id, status))
        elif status == "all":
            if channel_id:
                r1 = await self.db.execute("DELETE FROM questions WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
                r2 = await self.db.execute("DELETE FROM suggestions WHERE guild_id = ? AND target_channel_id = ?", (guild_id, channel_id))
            else:
                r1 = await self.db.execute("DELETE FROM questions WHERE guild_id = ?", (guild_id,))
                r2 = await self.db.execute("DELETE FROM suggestions WHERE guild_id = ?", (guild_id,))
            await self.db.commit()
            return r1.rowcount + r2.rowcount
        else:
            return 0
        await self.db.commit()
        return result.rowcount

    async def search_questions(self, guild_id: int, query_str: str, status: Optional[str] = None, channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
        escaped = query_str.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        if status and status in ("to_ask", "asked"):
            if channel_id:
                cursor = await self.db.execute(
                    "SELECT * FROM questions WHERE guild_id = ? AND channel_id = ? AND status = ? AND LOWER(question) LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 25",
                    (guild_id, channel_id, status, like_pattern),
                )
            else:
                cursor = await self.db.execute(
                    "SELECT * FROM questions WHERE guild_id = ? AND status = ? AND LOWER(question) LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 25",
                    (guild_id, status, like_pattern),
                )
        else:
            if channel_id:
                cursor = await self.db.execute(
                    "SELECT * FROM questions WHERE guild_id = ? AND channel_id = ? AND LOWER(question) LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 25",
                    (guild_id, channel_id, like_pattern),
                )
            else:
                cursor = await self.db.execute(
                    "SELECT * FROM questions WHERE guild_id = ? AND LOWER(question) LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 25",
                    (guild_id, like_pattern),
                )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_questions_page(self, guild_id: int, status: str, page: int = 0, page_size: int = 10, channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
        offset = page * page_size
        if channel_id:
            cursor = await self.db.execute(
                "SELECT * FROM questions WHERE guild_id = ? AND channel_id = ? AND status = ? ORDER BY id ASC LIMIT ? OFFSET ?",
                (guild_id, channel_id, status, page_size, offset),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM questions WHERE guild_id = ? AND status = ? ORDER BY id ASC LIMIT ? OFFSET ?",
                (guild_id, status, page_size, offset),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def question_page_count(self, guild_id: int, status: str, page_size: int = 10, channel_id: Optional[int] = None) -> int:
        if channel_id:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM questions WHERE guild_id = ? AND channel_id = ? AND status = ?",
                (guild_id, channel_id, status),
            )
        else:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM questions WHERE guild_id = ? AND status = ?",
                (guild_id, status),
            )
        total = (await cursor.fetchone())[0]
        return max(1, (total + page_size - 1) // page_size)

    async def get_suggestions_page(self, guild_id: int, page: int = 0, page_size: int = 10) -> List[Dict[str, Any]]:
        offset = page * page_size
        cursor = await self.db.execute(
            "SELECT * FROM suggestions WHERE guild_id = ? ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (guild_id, page_size, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def suggestions_page_count(self, guild_id: int, page_size: int = 10) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) FROM suggestions WHERE guild_id = ?", (guild_id,))
        total = (await cursor.fetchone())[0]
        return max(1, (total + page_size - 1) // page_size)

    async def get_suggestion(self, guild_id: int, suggestion_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute("SELECT * FROM suggestions WHERE guild_id = ? AND id = ?", (guild_id, suggestion_id))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def seed_default_questions(self, guild_id: int, channel_id: Optional[int] = None, lang: Optional[str] = None):
        if channel_id:
            cursor = await self.db.execute("SELECT COUNT(*) FROM questions WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
        else:
            cursor = await self.db.execute("SELECT COUNT(*) FROM questions WHERE guild_id = ?", (guild_id,))
        count = (await cursor.fetchone())[0]
        if count:
            return
        effective_lang = lang or self.get_language(guild_id)
        questions_list = DEFAULT_QUESTIONS.get(effective_lang, DEFAULT_QUESTIONS["en"])
        now = datetime.now(timezone.utc).isoformat()
        await self.db.executemany(
            "INSERT INTO questions (guild_id, channel_id, question, normalized_text, status, source, created_at) VALUES (?, ?, ?, ?, 'to_ask', 'default', ?)",
            [(guild_id, channel_id, q, normalize_question(q), now) for q in questions_list],
        )
        await self.db.commit()

    async def get_next_question(self, guild_id: int, channel_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        if channel_id:
            cursor = await self.db.execute(
                "SELECT * FROM questions WHERE guild_id = ? AND channel_id = ? AND status = 'to_ask' ORDER BY id ASC LIMIT 1",
                (guild_id, channel_id),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM questions WHERE guild_id = ? AND status = 'to_ask' ORDER BY id ASC LIMIT 1",
                (guild_id,),
            )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # -----------------------------------------------------------------------
    # Suggestion Contextual Routing & Review Pipeline
    # -----------------------------------------------------------------------

    async def resolve_suggestion_target(self, interaction: discord.Interaction) -> Tuple[str, Optional[int], List[Dict[str, Any]]]:
        """
        Determines the target QOTD channel for a suggestion:
        - "none": 0 channels configured (Scenario A)
        - "single": 1 channel configured (Scenario B)
        - "contextual": triggered inside an active QOTD channel (Scenario C)
        - "ambiguous": multiple channels configured, not inside one of them (Scenario D)
        """
        channels = await self.get_qotd_channels(interaction.guild_id)
        if not channels:
            return "none", None, []
        if len(channels) == 1:
            return "single", channels[0]["channel_id"], channels

        active_ids = [c["channel_id"] for c in channels]
        if interaction.channel_id in active_ids:
            return "contextual", interaction.channel_id, channels

        return "ambiguous", None, channels

    async def handle_user_suggestion(
        self,
        guild_id: int,
        user: Union[discord.User, discord.Member],
        question_text: str,
        note: Optional[str] = None,
        user_locale: Optional[Any] = None,
        lang: Optional[str] = None,
        target_channel_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Validates, creates, and dispatches a suggestion."""
        effective_lang = lang or (resolve_user_locale(user_locale) if user_locale else None) or self.get_server_language(guild_id)

        # Check if server has a role requirement for suggestions
        settings = await self.get_guild_settings(guild_id)
        suggest_role_id = settings.get("suggest_role_id")
        if suggest_role_id:
            is_allowed = False
            member = user if isinstance(user, discord.Member) else None
            if member is None:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    member = guild.get_member(user.id)
            if member:
                if getattr(member.guild_permissions, "administrator", False) or getattr(member.guild_permissions, "manage_guild", False):
                    is_allowed = True
                elif any(r.id == suggest_role_id for r in member.roles):
                    is_allowed = True
            if not is_allowed:
                return False, t(effective_lang, "sugg_role_required", role_id=suggest_role_id)

        parsed = parse_question_input(question_text)
        if not parsed:
            return False, t(effective_lang, "limit_empty_question")
        q = "\n".join(parsed)

        if len(q) > MAX_QUESTION_LENGTH:
            return False, t(effective_lang, "limit_question_too_long", max_len=MAX_QUESTION_LENGTH, len=len(q))

        # Server-wide mailbox limit
        guild_pending = await self.get_pending_suggestions_count(guild_id)
        if guild_pending >= MAX_PENDING_SUGGESTIONS:
            return False, t(effective_lang, "limit_sugg_mailbox_full", max_sugg=MAX_PENDING_SUGGESTIONS)

        # Per-user pending suggestions limit
        pending_count = await self.get_user_pending_suggestions_count(guild_id, user.id)
        if pending_count >= MAX_USER_PENDING_SUGGESTIONS:
            return False, t(effective_lang, "limit_user_pending", max_user=MAX_USER_PENDING_SUGGESTIONS)

        # Check for duplicates scoped to channel
        unique, _ = await self.check_duplicates(guild_id, [q], channel_id=target_channel_id)
        if not unique:
            return False, t(effective_lang, "duplicate_detected")

        suggestion_id = uuid.uuid4().hex[:16]
        now_str = datetime.now(timezone.utc).isoformat()
        avatar_url = str(user.display_avatar.url) if getattr(user, "display_avatar", None) else ""
        user_name_str = getattr(user, "display_name", None) or getattr(user, "name", None) or str(user)
        note_clean = (note or "").strip()[:500]
        stored_locale = resolve_user_locale(user_locale) if user_locale else (effective_lang if effective_lang in SUPPORTED_LANGUAGES else None)

        await self.db.execute(
            """
            INSERT INTO suggestions (id, guild_id, target_channel_id, question, normalized_text, message, avatar_url, user_id, user_name, created_at, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (suggestion_id, guild_id, target_channel_id, q, normalize_question(q), note_clean, avatar_url, user.id, user_name_str, now_str, stored_locale),
        )
        await self.db.commit()

        # Dispatch review message to admin channel if configured (in server language)
        await self.send_suggestion_for_review(guild_id, suggestion_id, {
            "id": suggestion_id,
            "guild_id": guild_id,
            "target_channel_id": target_channel_id,
            "question": q,
            "message": note_clean,
            "avatar_url": avatar_url,
            "user_id": user.id,
            "user_name": user_name_str,
            "created_at": now_str,
            "language": stored_locale,
        })

        return True, t(effective_lang, "sugg_sent_success")

    async def send_suggestion_for_review(self, guild_id: int, suggestion_id: str, suggestion: dict) -> bool:
        settings = await self.get_guild_settings(guild_id)
        admin_channel_id = settings.get("admin_channel_id")
        if not admin_channel_id:
            return False

        admin_channel = self.bot.get_channel(admin_channel_id)
        if admin_channel is None:
            try:
                admin_channel = await self.bot.fetch_channel(admin_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as err:
                logger.warning("Could not fetch admin channel %s: %s", admin_channel_id, err)
                return False

        if not isinstance(admin_channel, (discord.TextChannel, discord.Thread)):
            return False

        lang = self.get_server_language(guild_id)
        embed = discord.Embed(
            description=suggestion["question"],
            colour=discord.Colour.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=t(lang, "sugg_review_author", user=suggestion['user_name']),
            icon_url=suggestion.get("avatar_url") or None,
        )
        if suggestion.get("message"):
            embed.add_field(name=t(lang, "sugg_review_note"), value=suggestion["message"], inline=False)
        target_ch_id = suggestion.get("target_channel_id")
        if target_ch_id:
            embed.add_field(name=t(lang, "sugg_target_channel"), value=f"<#{target_ch_id}>", inline=True)
        embed.set_footer(text=t(lang, "sugg_review_pending"))

        try:
            msg = await admin_channel.send(
                embed=embed,
                view=suggestion_review_view(guild_id, suggestion_id, lang=lang),
            )
            await self.db.execute(
                "UPDATE suggestions SET review_message_id = ?, admin_channel_id = ? WHERE id = ?",
                (msg.id, admin_channel.id, suggestion_id),
            )
            await self.db.commit()
            return True
        except Exception as err:
            logger.error("Failed to send suggestion review card in guild %s: %s", guild_id, err)
            return False

    async def accept_suggestion(
        self,
        guild_id: int,
        suggestion_id: str,
        question: Optional[str] = None,
        review_message: Optional[discord.Message] = None,
        added_by: Optional[Union[discord.Member, discord.User]] = None,
        interaction: Optional[discord.Interaction] = None,
        target_channel_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        cursor = await self.db.execute("SELECT * FROM suggestions WHERE id = ? AND guild_id = ?", (suggestion_id, guild_id))
        pre_row = await cursor.fetchone()
        if not pre_row:
            return False, "not_found"

        channels = await self.get_qotd_channels(guild_id)
        if not channels:
            return False, "no_channels"
        active_ch_ids = [c["channel_id"] for c in channels]
        effective_target_ch = target_channel_id if target_channel_id is not None else pre_row["target_channel_id"]
        if effective_target_ch not in active_ch_ids:
            if len(channels) == 1:
                effective_target_ch = channels[0]["channel_id"]
            else:
                return False, "channel_invalid"

        queue_count = await self.get_queue_count(guild_id, channel_id=effective_target_ch)
        channel_max_limit = MAX_QUEUE_QUESTIONS
        ch_row = await self.get_qotd_channel(effective_target_ch)
        if ch_row and ch_row.get("max_queue_limit") is not None:
            channel_max_limit = ch_row["max_queue_limit"]
        if queue_count >= channel_max_limit:
            return False, "queue_full"

        total_count = await self.get_total_question_count(guild_id)
        if total_count >= MAX_TOTAL_QUESTIONS:
            return False, "total_limit"

        # Atomically claim the suggestion and insert into questions in the same transaction
        cursor = await self.db.execute(
            "DELETE FROM suggestions WHERE id = ? AND guild_id = ? RETURNING *",
            (suggestion_id, guild_id),
        )
        row = await cursor.fetchone()
        if not row:
            return False, "not_found"
        suggestion = dict(row)

        chosen_question = (question or suggestion["question"]).strip()[:MAX_QUESTION_LENGTH]
        added_by_name = (getattr(added_by, "display_name", None) or getattr(added_by, "name", None) or str(added_by)) if added_by else None
        if added_by_name is not None and not isinstance(added_by_name, str):
            added_by_name = str(added_by_name)
        now_str = datetime.now(timezone.utc).isoformat()

        await self.db.execute(
            """
            INSERT INTO questions (
                guild_id, channel_id, question, normalized_text, status, source, created_at,
                added_by_id, added_by_name, suggested_by_id, suggested_by_name
            ) VALUES (?, ?, ?, ?, 'to_ask', 'suggestion', ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                effective_target_ch,
                chosen_question,
                normalize_question(chosen_question),
                now_str,
                added_by.id if added_by else None,
                added_by_name,
                suggestion["user_id"],
                suggestion["user_name"],
            ),
        )
        await self.db.commit()

        server_lang = self.get_server_language(guild_id)

        # Update the admin channel message
        msg_to_update = review_message
        if not msg_to_update and suggestion.get("review_message_id") and suggestion.get("admin_channel_id"):
            ch = self.bot.get_channel(suggestion["admin_channel_id"])
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(suggestion["admin_channel_id"])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as err:
                    logger.debug("Could not fetch admin channel %s: %s", suggestion["admin_channel_id"], err)
                    ch = None
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg_to_update = await ch.fetch_message(suggestion["review_message_id"])
                except Exception as err:
                    logger.debug("Could not fetch review message %s: %s", suggestion["review_message_id"], err)

        base_emb = (
            review_message.embeds[0]
            if review_message and review_message.embeds
            else (msg_to_update.embeds[0] if msg_to_update and msg_to_update.embeds else None)
        )
        emb = None
        if base_emb:
            try:
                emb = discord.Embed.from_dict(base_emb.to_dict())
            except (KeyError, ValueError, TypeError) as err:
                logger.debug("Could not clone embed dict: %s", err)
                emb = None
        if not emb:
            emb = discord.Embed(title=t(server_lang, "sugg_detail_title"))
        emb.colour = discord.Colour.green()
        emb.set_footer(text=t(server_lang, "sugg_review_approved"))
        emb.description = chosen_question

        if interaction and not interaction.is_expired():
            try:
                await interaction.edit_original_response(embed=emb, view=None)
                msg_to_update = None
            except Exception as err:
                logger.debug("Failed to edit review message via interaction: %s", err)

        if msg_to_update and msg_to_update.embeds:
            try:
                await msg_to_update.edit(embed=emb, view=None)
            except Exception as err:
                logger.debug("Failed to edit review message directly: %s", err)

        # Check if suggestion was edited
        orig_q = suggestion["question"].strip()
        was_edited = (chosen_question != orig_q) or (target_channel_id is not None and target_channel_id != suggestion.get("target_channel_id"))

        # Notify suggester via DM (in suggester's language)
        try:
            user = self.bot.get_user(suggestion["user_id"]) or await self.bot.fetch_user(suggestion["user_id"])
            guild = self.bot.get_guild(guild_id)
            user_dm_lang = suggestion.get("language") or server_lang

            ch_obj = self.bot.get_channel(effective_target_ch) if effective_target_ch else None
            ch_name = f"#{ch_obj.name}" if ch_obj else ""

            if was_edited:
                if ch_name:
                    dm_desc = t(user_dm_lang, "sugg_dm_edited_desc_chan", question=chosen_question, orig_question=orig_q, channel=ch_name)
                else:
                    dm_desc = t(user_dm_lang, "sugg_dm_edited_desc", question=chosen_question, orig_question=orig_q)
                dm_title = t(user_dm_lang, "sugg_dm_edited_title")
            else:
                if ch_name:
                    dm_desc = t(user_dm_lang, "sugg_dm_approved_desc_chan", question=chosen_question, channel=ch_name)
                else:
                    dm_desc = t(user_dm_lang, "sugg_dm_approved_desc", question=chosen_question)
                dm_title = t(user_dm_lang, "sugg_dm_approved_title")

            dm_embed = discord.Embed(
                title=dm_title,
                description=dm_desc,
                colour=discord.Colour.green(),
                timestamp=datetime.now(timezone.utc),
            )
            if guild:
                dm_embed.set_footer(text=f"{guild.name} • QOTD")
            await user.send(embed=dm_embed)
        except Exception as err:
            logger.debug("Could not DM suggester %s: %s", suggestion["user_id"], err)

        return True, "ok"

    async def reject_suggestion(
        self,
        guild_id: int,
        suggestion_id: str,
        reason: str,
        review_message: Optional[discord.Message] = None,
        interaction: Optional[discord.Interaction] = None,
    ) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM suggestions WHERE id = ? AND guild_id = ? RETURNING *",
            (suggestion_id, guild_id),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        suggestion = dict(row)
        await self.db.commit()

        server_lang = self.get_server_language(guild_id)
        target_channel_id = suggestion.get("target_channel_id")

        msg_to_update = review_message
        if not msg_to_update and suggestion.get("review_message_id") and suggestion.get("admin_channel_id"):
            ch = self.bot.get_channel(suggestion["admin_channel_id"])
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(suggestion["admin_channel_id"])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as err:
                    logger.debug("Could not fetch admin channel %s: %s", suggestion["admin_channel_id"], err)
                    ch = None
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg_to_update = await ch.fetch_message(suggestion["review_message_id"])
                except Exception as err:
                    logger.debug("Could not fetch reject message %s: %s", suggestion["review_message_id"], err)

        base_emb = (
            review_message.embeds[0]
            if review_message and review_message.embeds
            else (msg_to_update.embeds[0] if msg_to_update and msg_to_update.embeds else None)
        )
        emb = None
        if base_emb:
            try:
                emb = discord.Embed.from_dict(base_emb.to_dict())
            except (KeyError, ValueError, TypeError) as err:
                logger.debug("Could not clone embed dict: %s", err)
                emb = None
        if not emb:
            emb = discord.Embed(title=t(server_lang, "sugg_detail_title"), description=suggestion["question"])
        emb.colour = discord.Colour.red()
        emb.set_footer(text="❌ " + (t(server_lang, "btn_decline")))
        if reason:
            emb.add_field(name=t(server_lang, "sugg_dm_rejected_reason"), value=reason, inline=False)

        if interaction and not interaction.is_expired():
            try:
                await interaction.edit_original_response(embed=emb, view=None)
                msg_to_update = None
            except Exception as err:
                logger.debug("Failed to edit reject message via interaction: %s", err)

        if msg_to_update and msg_to_update.embeds:
            try:
                await msg_to_update.edit(embed=emb, view=None)
            except Exception as err:
                logger.debug("Failed to edit reject message directly: %s", err)

        try:
            user = self.bot.get_user(suggestion["user_id"]) or await self.bot.fetch_user(suggestion["user_id"])
            guild = self.bot.get_guild(guild_id)
            user_dm_lang = suggestion.get("language") or server_lang

            ch_obj = self.bot.get_channel(target_channel_id) if target_channel_id else None
            ch_name = f"#{ch_obj.name}" if ch_obj else ""

            if ch_name:
                dm_desc = t(user_dm_lang, "sugg_dm_rejected_desc_chan", question=suggestion["question"], channel=ch_name)
            else:
                dm_desc = t(user_dm_lang, "sugg_dm_rejected_desc", question=suggestion["question"])

            dm_embed = discord.Embed(
                title=t(user_dm_lang, "sugg_dm_rejected_title"),
                description=dm_desc,
                colour=discord.Colour.red(),
                timestamp=datetime.now(timezone.utc),
            )
            if reason:
                dm_embed.add_field(name=t(user_dm_lang, "sugg_dm_rejected_reason"), value=reason, inline=False)
            if guild:
                dm_embed.set_footer(text=f"{guild.name} • QOTD")
            await user.send(embed=dm_embed)
        except Exception as err:
            logger.debug("Could not DM suggester %s on reject: %s", suggestion["user_id"], err)

        return True

    # -----------------------------------------------------------------------
    # Embed Builders
    # -----------------------------------------------------------------------

