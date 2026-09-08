import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
import aiosqlite
import discord
import pytest

from cogs.qotd.cog import Qotd
from cogs.qotd.constants import (
    MAX_QUEUE_QUESTIONS,
    MAX_TOTAL_QUESTIONS,
    MAX_FILE_UPLOAD_QUESTIONS,
    MAX_BATCH_ADD_QUESTIONS,
)
from cogs.qotd.modals import AddQuestionModal


@pytest.mark.asyncio
async def test_add_multiple_questions_atomic_queue_limit(in_memory_db: aiosqlite.Connection):
    """Verify add_multiple_questions respects channel max_queue_limit and skips overflow."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    # Channel with max_queue_limit = 3
    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 3, ?)",
        (guild_id, channel_id, now_iso),
    )
    # Pre-populate 1 question
    await in_memory_db.execute(
        "INSERT INTO questions (guild_id, channel_id, question, status, created_at) VALUES (?, ?, 'Q1', 'to_ask', ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    # Try adding 4 questions (only 2 slots available)
    batch = ["Q2", "Q3", "Q4", "Q5"]
    added, skipped, reason = await cog.add_multiple_questions(guild_id, batch, channel_id=channel_id)

    assert len(added) == 2
    assert skipped == 2
    assert reason == "queue_full"

    # Verify queue count is exactly 3
    q_count = await cog.get_queue_count(guild_id, channel_id=channel_id)
    assert q_count == 3


@pytest.mark.asyncio
async def test_add_multiple_questions_total_limit(in_memory_db: aiosqlite.Connection):
    """Verify add_multiple_questions respects MAX_TOTAL_QUESTIONS across all channels."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 500, ?)",
        (guild_id, channel_id, now_iso),
    )
    # Pre-populate MAX_TOTAL_QUESTIONS - 2 questions in history
    for i in range(MAX_TOTAL_QUESTIONS - 2):
        await in_memory_db.execute(
            "INSERT INTO questions (guild_id, channel_id, question, status, created_at) VALUES (?, ?, ?, 'asked', ?)",
            (guild_id, channel_id, f"Hist {i}", now_iso),
        )
    await in_memory_db.commit()

    # Try adding 5 questions (only 2 slots available before total limit)
    batch = ["New 1", "New 2", "New 3", "New 4", "New 5"]
    added, skipped, reason = await cog.add_multiple_questions(guild_id, batch, channel_id=channel_id)

    assert len(added) == 2
    assert skipped == 3
    assert reason == "total_limit"

    tot_count = await cog.get_total_question_count(guild_id)
    assert tot_count == MAX_TOTAL_QUESTIONS


@pytest.mark.asyncio
async def test_accept_suggestion_atomic_prevents_data_loss(in_memory_db: aiosqlite.Connection):
    """Verify that if accept_suggestion fails due to full queue, suggestion is NOT deleted."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_server_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    # Channel with max limit = 1
    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 1, ?)",
        (guild_id, channel_id, now_iso),
    )
    # 1 question already in queue
    await in_memory_db.execute(
        "INSERT INTO questions (guild_id, channel_id, question, status, created_at) VALUES (?, ?, 'Existing', 'to_ask', ?)",
        (guild_id, channel_id, now_iso),
    )
    # 1 pending suggestion
    await in_memory_db.execute(
        "INSERT INTO suggestions (id, guild_id, target_channel_id, question, user_id, user_name, created_at) VALUES ('sugg-1', ?, ?, 'Sugg Q', 123, 'Bob', ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    # Attempt to accept suggestion
    ok, reason = await cog.accept_suggestion(guild_id, "sugg-1", target_channel_id=channel_id)
    assert ok is False
    assert reason == "queue_full"

    # Suggestion must still exist in database
    cursor = await in_memory_db.execute("SELECT * FROM suggestions WHERE id = 'sugg-1'")
    sugg = await cursor.fetchone()
    assert sugg is not None, "Suggestion must NOT be deleted if queue is full"


@pytest.mark.asyncio
async def test_accept_suggestion_unlinked_channel_handling(in_memory_db: aiosqlite.Connection):
    """Verify accept_suggestion rejects when target channel is unlinked and multiple channels exist."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_server_language = MagicMock(return_value="en")

    guild_id = 100
    now_iso = datetime.now().isoformat()

    # Two active channels (201, 202)
    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, 201, '09:00', 500, ?)",
        (guild_id, now_iso),
    )
    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, 202, '10:00', 500, ?)",
        (guild_id, now_iso),
    )
    # Suggestion targeted for non-existent channel 999
    await in_memory_db.execute(
        "INSERT INTO suggestions (id, guild_id, target_channel_id, question, user_id, user_name, created_at) VALUES ('sugg-unlinked', ?, 999, 'Unlinked Q', 123, 'Bob', ?)",
        (guild_id, now_iso),
    )
    await in_memory_db.commit()

    ok, reason = await cog.accept_suggestion(guild_id, "sugg-unlinked")
    assert ok is False
    assert reason == "channel_invalid"

    # Suggestion remains intact
    cursor = await in_memory_db.execute("SELECT * FROM suggestions WHERE id = 'sugg-unlinked'")
    assert (await cursor.fetchone()) is not None


@pytest.mark.asyncio
async def test_requeue_question_resolves_channel_and_links(in_memory_db: aiosqlite.Connection):
    """Verify requeue_question updates channel_id if previously NULL and checks channel limit."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 5, ?)",
        (guild_id, channel_id, now_iso),
    )
    # Question with channel_id NULL in asked status
    cursor = await in_memory_db.execute(
        "INSERT INTO questions (guild_id, channel_id, question, status, created_at) VALUES (?, NULL, 'Legacy Q', 'asked', ?)",
        (guild_id, now_iso),
    )
    qid = cursor.lastrowid
    await in_memory_db.commit()

    # Requeue question specifying target channel
    ok = await cog.requeue_question(guild_id, qid, channel_id=channel_id)
    assert ok is True

    # Check question updated to to_ask and channel_id set
    cursor = await in_memory_db.execute("SELECT channel_id, status FROM questions WHERE id = ?", (qid,))
    row = await cursor.fetchone()
    assert row[0] == channel_id
    assert row[1] == "to_ask"


@pytest.mark.asyncio
async def test_add_question_modal_no_channels_configured(in_memory_db: aiosqlite.Connection):
    """Verify AddQuestionModal rejects submission if no QOTD channels are configured."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100

    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=None, lang="en")
    modal.questions_input._value = "Will this add?"

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "no qotd channels" in msg.lower() or "configured" in msg.lower()


def test_add_question_modal_file_upload_structure():
    """Verify AddQuestionModal contains the FileUpload component wrapped in Label."""
    cog = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")
    modal = AddQuestionModal(cog, guild_id=123, lang="en")

    assert hasattr(modal, "file_upload")
    assert isinstance(modal.file_upload, discord.ui.FileUpload)
    assert modal.file_upload.required is False
    assert modal.file_upload.max_values == 1
    assert hasattr(modal, "file_label")
    assert isinstance(modal.file_label, discord.ui.Label)
    assert modal.file_label.component is modal.file_upload
    assert modal.questions_input.required is False


@pytest.mark.asyncio
async def test_add_question_modal_upload_txt_file_success(in_memory_db: aiosqlite.Connection):
    """Verify AddQuestionModal parses questions from uploaded .txt file and adds them to DB."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 500, ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=channel_id, lang="en")
    modal.questions_input._value = ""

    # Mock attachment
    mock_attachment = MagicMock(spec=discord.Attachment)
    mock_attachment.filename = "my_questions.txt"
    mock_attachment.size = 1024
    mock_attachment.read = AsyncMock(return_value=b"1. File Question One?\n2. File Question Two?\n- File Question Three?")
    modal.file_upload._values = [mock_attachment]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "3" in msg  # 3 questions added

    # Verify rows in database
    q_count = await cog.get_queue_count(guild_id, channel_id=channel_id)
    assert q_count == 3


@pytest.mark.asyncio
async def test_add_question_modal_upload_txt_file_invalid_extension(in_memory_db: aiosqlite.Connection):
    """Verify AddQuestionModal rejects files that do not have a .txt extension."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 500, ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=channel_id, lang="en")
    modal.questions_input._value = ""

    mock_attachment = MagicMock(spec=discord.Attachment)
    mock_attachment.filename = "image.png"
    mock_attachment.size = 2048
    mock_attachment.read = AsyncMock(return_value=b"fake image data")
    modal.file_upload._values = [mock_attachment]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert ".txt" in msg


@pytest.mark.asyncio
async def test_add_question_modal_combined_text_and_file(in_memory_db: aiosqlite.Connection):
    """Verify AddQuestionModal combines questions from both text input and uploaded .txt file."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 500, ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=channel_id, lang="en")
    modal.questions_input._value = "TextInput Question 1?\nTextInput Question 2?"

    mock_attachment = MagicMock(spec=discord.Attachment)
    mock_attachment.filename = "more_questions.txt"
    mock_attachment.size = 100
    mock_attachment.read = AsyncMock(return_value=b"File Question 3?")
    modal.file_upload._values = [mock_attachment]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "3" in msg  # 2 from text + 1 from file = 3

    q_count = await cog.get_queue_count(guild_id, channel_id=channel_id)
    assert q_count == 3


@pytest.mark.asyncio
async def test_add_question_modal_upload_txt_file_over_500_limit(in_memory_db: aiosqlite.Connection):
    """Verify AddQuestionModal rejects .txt file containing more than 500 questions."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 500, ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=channel_id, lang="en")
    modal.questions_input._value = ""

    # 501 questions
    content = "\n".join(f"Question number {i}?" for i in range(1, 502)).encode("utf-8")
    mock_attachment = MagicMock(spec=discord.Attachment)
    mock_attachment.filename = "large_batch.txt"
    mock_attachment.size = len(content)
    mock_attachment.read = AsyncMock(return_value=content)
    modal.file_upload._values = [mock_attachment]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "500" in msg
    assert "501" in msg

    q_count = await cog.get_queue_count(guild_id, channel_id=channel_id)
    assert q_count == 0


@pytest.mark.asyncio
async def test_add_question_modal_upload_txt_file_500_questions_accepted(in_memory_db: aiosqlite.Connection):
    """Verify AddQuestionModal accepts .txt file with exactly 500 questions."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 500, ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=channel_id, lang="en")
    modal.questions_input._value = ""

    # Exactly 500 questions
    content = "\n".join(f"Question number {i}?" for i in range(1, 501)).encode("utf-8")
    mock_attachment = MagicMock(spec=discord.Attachment)
    mock_attachment.filename = "500_questions.txt"
    mock_attachment.size = len(content)
    mock_attachment.read = AsyncMock(return_value=content)
    modal.file_upload._values = [mock_attachment]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "500" in msg

    q_count = await cog.get_queue_count(guild_id, channel_id=channel_id)
    assert q_count == 500


@pytest.mark.asyncio
async def test_add_question_modal_text_paste_over_50_limit(in_memory_db: aiosqlite.Connection):
    """Verify AddQuestionModal rejects text input with more than 50 questions."""
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 100
    channel_id = 200
    now_iso = datetime.now().isoformat()

    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 500, ?)",
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=channel_id, lang="en")
    # 51 questions in text input
    modal.questions_input._value = "\n".join(f"Text question {i}?" for i in range(1, 52))
    modal.file_upload._values = []

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args[0][0]
    assert "50" in msg
    assert "51" in msg

    q_count = await cog.get_queue_count(guild_id, channel_id=channel_id)
    assert q_count == 0

