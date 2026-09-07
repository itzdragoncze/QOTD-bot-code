from datetime import datetime, date, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo
import aiosqlite
import discord
import pytest

from cogs.qotd.constants import (
    VALID_CHANNEL_COLS,
    VALID_SETTINGS_COLS,
    QOTD_TIMEZONE,
    get_next_qotd_datetime,
)
from cogs.qotd.cog import Qotd
from cogs.qotd.modals import EditSuggestionModal


def test_column_whitelists():
    assert "max_queue_limit" in VALID_CHANNEL_COLS
    assert "suggest_role_id" in VALID_SETTINGS_COLS


def test_get_next_qotd_datetime():
    # Case 1: Before scheduled time today (08:30 < 09:00)
    now = datetime(2026, 9, 7, 8, 30, tzinfo=QOTD_TIMEZONE)
    chan = {"scheduled_time": "09:00", "last_posted_date": None}
    next_dt = get_next_qotd_datetime(chan, now=now)
    assert next_dt == datetime(2026, 9, 7, 9, 0, tzinfo=QOTD_TIMEZONE)

    # Case 2: After scheduled time today (09:30 >= 09:00)
    now = datetime(2026, 9, 7, 9, 30, tzinfo=QOTD_TIMEZONE)
    chan = {"scheduled_time": "09:00", "last_posted_date": None}
    next_dt = get_next_qotd_datetime(chan, now=now)
    assert next_dt == datetime(2026, 9, 8, 9, 0, tzinfo=QOTD_TIMEZONE)

    # Case 3: Before scheduled time today, but already posted today
    now = datetime(2026, 9, 7, 8, 30, tzinfo=QOTD_TIMEZONE)
    chan = {"scheduled_time": "09:00", "last_posted_date": "2026-09-07"}
    next_dt = get_next_qotd_datetime(chan, now=now)
    assert next_dt == datetime(2026, 9, 8, 9, 0, tzinfo=QOTD_TIMEZONE)

    # Case 4: Hour only formatted string
    now = datetime(2026, 9, 7, 10, 0, tzinfo=QOTD_TIMEZONE)
    chan = {"scheduled_time": "14", "last_posted_date": None}
    next_dt = get_next_qotd_datetime(chan, now=now)
    assert next_dt == datetime(2026, 9, 7, 14, 0, tzinfo=QOTD_TIMEZONE)


@pytest.mark.asyncio
async def test_max_queue_limit_enforced_in_db(in_memory_db: aiosqlite.Connection):
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()

    guild_id = 1001
    channel_id = 2001

    # Insert channel with max_queue_limit = 2
    now_iso = datetime.now().isoformat()
    await in_memory_db.execute(
        """
        INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at)
        VALUES (?, ?, '09:00', 2, ?)
        """,
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    # Add 1st question
    q1 = await cog.add_question(guild_id, "Q1?", channel_id=channel_id)
    assert q1 is not None

    # Add 2nd question
    q2 = await cog.add_question(guild_id, "Q2?", channel_id=channel_id)
    assert q2 is not None

    # Add 3rd question (exceeds max_queue_limit of 2)
    q3 = await cog.add_question(guild_id, "Q3?", channel_id=channel_id)
    assert q3 is None


@pytest.mark.asyncio
async def test_accept_suggestion_target_channel_and_edit_dm(in_memory_db: aiosqlite.Connection):
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.get_server_language = MagicMock(return_value="en")

    # Mock user for DM
    mock_user = MagicMock(spec=discord.User)
    mock_user.id = 4001
    mock_user.send = AsyncMock()

    # Mock channel
    mock_chan = MagicMock(spec=discord.TextChannel)
    mock_chan.id = 2002
    mock_chan.name = "general-qotd"

    mock_bot = MagicMock()
    mock_bot.get_user = MagicMock(return_value=mock_user)
    mock_bot.get_channel = MagicMock(return_value=mock_chan)
    mock_bot.get_guild = MagicMock(return_value=MagicMock(name="Test Guild"))
    cog.bot = mock_bot

    guild_id = 1001
    orig_chan_id = 2001
    new_chan_id = 2002
    sugg_id = "sugg-abc"

    # Insert channel rows
    now_iso = datetime.now().isoformat()
    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 10, ?)",
        (guild_id, new_chan_id, now_iso),
    )
    # Insert suggestion
    await in_memory_db.execute(
        """
        INSERT INTO suggestions (id, guild_id, target_channel_id, question, user_id, user_name, created_at, language)
        VALUES (?, ?, ?, 'Original question text?', 4001, 'Suggester', ?, 'en')
        """,
        (sugg_id, guild_id, orig_chan_id, now_iso),
    )
    await in_memory_db.commit()

    # Accept suggestion with edited text AND new target_channel_id
    success, reason = await cog.accept_suggestion(
        guild_id,
        sugg_id,
        question="Edited question text?",
        target_channel_id=new_chan_id,
    )
    assert success is True
    assert reason == "ok"

    # Verify question inserted with new_chan_id and edited text
    cur = await in_memory_db.execute("SELECT * FROM questions WHERE guild_id = ? AND channel_id = ?", (guild_id, new_chan_id))
    q_row = await cur.fetchone()
    assert q_row is not None
    assert q_row["question"] == "Edited question text?"
    assert q_row["channel_id"] == new_chan_id
    assert q_row["suggested_by_id"] == 4001

    # Verify suggester received an edited DM
    assert mock_user.send.call_count == 1
    call_args = mock_user.send.call_args[1]
    embed = call_args["embed"]
    assert "edited" in embed.title.lower()
    assert "*Original:*" in embed.description
    assert "#general-qotd" in embed.description


@pytest.mark.asyncio
async def test_handle_user_suggestion_role_requirement(in_memory_db: aiosqlite.Connection):
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.get_server_language = MagicMock(return_value="en")
    cog.check_duplicates = AsyncMock(return_value=(["Valid question?"], []))
    cog.send_suggestion_for_review = AsyncMock()

    guild_id = 1001
    required_role_id = 9999

    # Insert settings with suggest_role_id
    await in_memory_db.execute(
        "INSERT INTO settings (guild_id, suggest_role_id) VALUES (?, ?)",
        (guild_id, required_role_id),
    )
    await in_memory_db.commit()

    # User 1: Member without required role and without admin
    role_other = MagicMock()
    role_other.id = 1111
    user_no_role = MagicMock(spec=discord.Member)
    user_no_role.id = 5001
    user_no_role.display_name = "UserNoRole"
    user_no_role.roles = [role_other]
    user_no_role.guild_permissions.administrator = False
    user_no_role.guild_permissions.manage_guild = False

    ok, msg = await cog.handle_user_suggestion(guild_id, user_no_role, "Question from user without role?")
    assert ok is False
    assert f"<@&{required_role_id}>" in msg

    # User 2: Member with required role
    role_req = MagicMock()
    role_req.id = required_role_id
    user_with_role = MagicMock(spec=discord.Member)
    user_with_role.id = 5002
    user_with_role.display_name = "UserWithRole"
    user_with_role.roles = [role_req]
    user_with_role.guild_permissions.administrator = False
    user_with_role.guild_permissions.manage_guild = False

    ok2, msg2 = await cog.handle_user_suggestion(guild_id, user_with_role, "Question from user with role?")
    assert ok2 is True

    # User 3: Admin member without the specific role
    user_admin = MagicMock(spec=discord.Member)
    user_admin.id = 5003
    user_admin.display_name = "AdminUser"
    user_admin.roles = []
    user_admin.guild_permissions.administrator = True
    user_admin.guild_permissions.manage_guild = True

    ok3, msg3 = await cog.handle_user_suggestion(guild_id, user_admin, "Question from admin user?")
    assert ok3 is True


@pytest.mark.asyncio
async def test_edit_suggestion_modal_channel_selection():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.accept_suggestion = AsyncMock(return_value=(True, "ok"))
    qotd.suggestions_page_count = AsyncMock(return_value=1)
    qotd.build_suggestions_embed = AsyncMock(return_value=MagicMock())
    qotd.get_suggestions_page = AsyncMock(return_value=[])
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    channels = [{"channel_id": 2001}, {"channel_id": 2002}]
    modal = EditSuggestionModal(
        qotd,
        guild_id=1,
        suggestion_id="sugg-1",
        current_text="Old question",
        channels=channels,
        target_channel_id=2002,
        from_panel=True,
    )

    assert modal.channel_select is not None
    assert len(modal.channel_select.options) == 2
    assert modal.channel_select.options[1].value == "2002"
    assert modal.channel_select.options[1].default is True

    # Simulate user changing select value to 2001 and submitting
    modal.channel_select._values = ["2001"]
    modal.question_input._value = "Updated question text"

    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.guild_permissions.manage_guild = True
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    qotd.accept_suggestion.assert_awaited_once_with(
        1,
        "sugg-1",
        question="Updated question text",
        review_message=None,
        added_by=interaction.user,
        interaction=None,
        target_channel_id=2001,
    )


@pytest.mark.asyncio
async def test_qotd_add_slash_command_respects_channel_max_queue_limit(in_memory_db: aiosqlite.Connection):
    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 1001
    channel_id = 2001

    # Insert channel with max_queue_limit = 1
    now_iso = datetime.now().isoformat()
    await in_memory_db.execute(
        """
        INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at)
        VALUES (?, ?, '09:00', 1, ?)
        """,
        (guild_id, channel_id, now_iso),
    )
    # Insert 1 question so queue is at limit (1/1)
    await in_memory_db.execute(
        """
        INSERT INTO questions (guild_id, channel_id, question, status, created_at)
        VALUES (?, ?, 'Existing Question?', 'to_ask', ?)
        """,
        (guild_id, channel_id, now_iso),
    )
    await in_memory_db.commit()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.channel_id = channel_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.qotd_add.callback(cog, interaction, question="Another question?")

    interaction.followup.send.assert_awaited_once()
    sent_msg = interaction.followup.send.call_args[0][0]
    assert "1" in sent_msg


@pytest.mark.asyncio
async def test_overreached_channel_queue_limit_blocks_all_addition_methods(in_memory_db: aiosqlite.Connection):
    from cogs.qotd.modals import AddQuestionModal

    cog = Qotd.__new__(Qotd)
    cog.db = in_memory_db
    cog.bot = MagicMock()
    cog.get_user_language = MagicMock(return_value="en")

    guild_id = 1001
    channel_id = 2001

    # Channel limit is 2, but there are already 3 questions (overreached!)
    now_iso = datetime.now().isoformat()
    await in_memory_db.execute(
        "INSERT INTO qotd_channels (guild_id, channel_id, scheduled_time, max_queue_limit, created_at) VALUES (?, ?, '09:00', 2, ?)",
        (guild_id, channel_id, now_iso),
    )
    for i in range(3):
        await in_memory_db.execute(
            "INSERT INTO questions (guild_id, channel_id, question, status, created_at) VALUES (?, ?, ?, 'to_ask', ?)",
            (guild_id, channel_id, f"Existing Q{i}", now_iso),
        )
    await in_memory_db.commit()

    # 1. Direct add_question must reject (return None)
    res = await cog.add_question(guild_id, "New Question?", channel_id=channel_id)
    assert res is None

    # 2. AddQuestionModal must reject
    modal = AddQuestionModal(cog, guild_id, current_page=0, channel_id=channel_id, lang="en")
    modal.questions_input._value = "Modal Question?"

    inter_modal = MagicMock(spec=discord.Interaction)
    inter_modal.user = MagicMock(spec=discord.Member)
    inter_modal.user.guild_permissions.manage_guild = True
    inter_modal.response.defer = AsyncMock()
    inter_modal.followup.send = AsyncMock()

    await modal.on_submit(inter_modal)
    inter_modal.followup.send.assert_awaited_once()
    assert "full" in inter_modal.followup.send.call_args[0][0].lower()

    # 3. /qotd_add slash command must reject
    inter_cmd = MagicMock(spec=discord.Interaction)
    inter_cmd.guild_id = guild_id
    inter_cmd.channel_id = channel_id
    inter_cmd.user = MagicMock(spec=discord.Member)
    inter_cmd.response.defer = AsyncMock()
    inter_cmd.followup.send = AsyncMock()

    await cog.qotd_add.callback(cog, inter_cmd, question="Slash Question?")
    inter_cmd.followup.send.assert_awaited_once()
    assert "full" in inter_cmd.followup.send.call_args[0][0].lower()



