from datetime import datetime, timezone
import aiosqlite
import pytest


from cogs.qotd.constants import VALID_CHANNEL_COLS, VALID_SETTINGS_COLS


@pytest.mark.asyncio
async def test_channel_crud_and_whitelist(in_memory_db: aiosqlite.Connection):
    # Insert channel
    now_iso = datetime.now(timezone.utc).isoformat()
    await in_memory_db.execute(
        """
        INSERT INTO qotd_channels (guild_id, channel_id, role_id, scheduled_time, created_at)
        VALUES (1001, 2001, 3001, '10:00', ?)
        """,
        (now_iso,),
    )
    await in_memory_db.commit()

    # Query channel
    cur = await in_memory_db.execute("SELECT * FROM qotd_channels WHERE channel_id = 2001")
    row = await cur.fetchone()
    assert row is not None
    assert row["guild_id"] == 1001
    assert row["scheduled_time"] == "10:00"

    # Whitelist validation test
    kwargs = {"scheduled_time": "14:30", "qotd_number": 5}
    invalid = set(kwargs.keys()) - VALID_CHANNEL_COLS
    assert not invalid

    # Attempting invalid column must be caught by whitelist
    invalid_kwargs = {"scheduled_time": "14:30", "malicious_col": "DROP TABLE"}
    invalid = set(invalid_kwargs.keys()) - VALID_CHANNEL_COLS
    assert "malicious_col" in invalid


@pytest.mark.asyncio
async def test_question_queue_lifecycle(in_memory_db: aiosqlite.Connection):
    now_iso = datetime.now(timezone.utc).isoformat()

    # Insert questions into queue
    await in_memory_db.execute(
        """
        INSERT INTO questions (guild_id, channel_id, question, normalized_text, status, source, created_at)
        VALUES (1001, 2001, 'Question 1?', 'question 1', 'to_ask', 'manual', ?)
        """,
        (now_iso,),
    )
    await in_memory_db.execute(
        """
        INSERT INTO questions (guild_id, channel_id, question, normalized_text, status, source, created_at)
        VALUES (1001, 2001, 'Question 2?', 'question 2', 'to_ask', 'manual', ?)
        """,
        (now_iso,),
    )
    await in_memory_db.commit()

    # Verify queue count
    cur = await in_memory_db.execute(
        "SELECT COUNT(*) FROM questions WHERE guild_id = 1001 AND channel_id = 2001 AND status = 'to_ask'"
    )
    count = (await cur.fetchone())[0]
    assert count == 2

    # Mark Question 1 as asked
    await in_memory_db.execute(
        "UPDATE questions SET status = 'asked', asked_at = ? WHERE guild_id = 1001 AND question = 'Question 1?'",
        (now_iso,),
    )
    await in_memory_db.commit()

    # Verify queue count updated
    cur = await in_memory_db.execute(
        "SELECT COUNT(*) FROM questions WHERE guild_id = 1001 AND channel_id = 2001 AND status = 'to_ask'"
    )
    assert (await cur.fetchone())[0] == 1

    # Verify history count
    cur = await in_memory_db.execute(
        "SELECT COUNT(*) FROM questions WHERE guild_id = 1001 AND channel_id = 2001 AND status = 'asked'"
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_settings_crud_and_whitelist(in_memory_db: aiosqlite.Connection):
    # Insert settings
    await in_memory_db.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (1001)")
    await in_memory_db.commit()

    # Valid update
    valid_update = {"admin_channel_id": 9999, "language": "cs"}
    invalid = set(valid_update.keys()) - VALID_SETTINGS_COLS
    assert not invalid

    set_clause = ", ".join(f"{col} = ?" for col in valid_update)
    values = list(valid_update.values()) + [1001]
    await in_memory_db.execute(f"UPDATE settings SET {set_clause} WHERE guild_id = ?", values)
    await in_memory_db.commit()

    cur = await in_memory_db.execute("SELECT * FROM settings WHERE guild_id = 1001")
    row = await cur.fetchone()
    assert row["admin_channel_id"] == 9999
    assert row["language"] == "cs"
