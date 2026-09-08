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
    Icon,
)
from cogs.qotd.cog import Qotd
from cogs.qotd.modals import EditSuggestionModal, AddQotdChannelModal, GuildSettingsModal, ChannelSettingsModal, UnlinkChannelModal
from cogs.qotd.views import QotdPanelView, ChannelManageView



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


def test_add_qotd_channel_modal_structure():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = AddQotdChannelModal(qotd, guild_id=123, lang="en")

    assert modal.channel_select is not None
    assert modal.channel_select.channel_types == [discord.ChannelType.text]
    assert modal.channel_select.required is True
    assert modal.channel_label.component is modal.channel_select

    assert modal.schedule_input is not None
    assert modal.schedule_input.default is None
    assert modal.schedule_input.placeholder == "Example: 12:34"
    assert modal.schedule_input.required is True
    assert modal.schedule_input.min_length == 5
    assert modal.schedule_input.max_length == 5

    assert modal.role_select is not None
    assert modal.role_select.min_values == 0
    assert modal.role_select.required is False
    assert modal.role_label.component is modal.role_select

    payload = modal.to_dict()
    assert payload["title"] == "Add QOTD Channel"
    assert len(payload["components"]) == 3
    assert payload["components"][0]["type"] == 18  # Label (ChannelSelect)
    assert payload["components"][0]["component"]["type"] == 8  # ChannelSelect
    assert payload["components"][1]["type"] == 1   # ActionRow (TextInput)
    ti_comp = payload["components"][1]["components"][0]
    assert ti_comp["required"] is True
    assert ti_comp["min_length"] == 5
    assert ti_comp["max_length"] == 5
    assert payload["components"][2]["type"] == 18  # Label (RoleSelect)
    assert payload["components"][2]["component"]["type"] == 6  # RoleSelect


@pytest.mark.asyncio
async def test_add_qotd_channel_modal_admin_check():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = AddQotdChannelModal(qotd, guild_id=123, lang="en")
    modal.channel_select._values = ["2001"]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = False
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "administrators" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_add_qotd_channel_modal_duplicate():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.get_qotd_channel = AsyncMock(return_value={"channel_id": 2001})
    modal = AddQotdChannelModal(qotd, guild_id=123, lang="en")
    modal.channel_select._values = ["2001"]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "already registered" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_add_qotd_channel_modal_time_invalid():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.get_qotd_channel = AsyncMock(return_value=None)
    modal = AddQotdChannelModal(qotd, guild_id=123, lang="en")
    modal.channel_select._values = ["2001"]
    modal.schedule_input._value = "25:99"

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "invalid time format" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_add_qotd_channel_modal_success():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.get_qotd_channel = AsyncMock(return_value=None)
    qotd.add_qotd_channel = AsyncMock()
    qotd.build_settings_embed = AsyncMock(return_value=MagicMock())
    qotd.get_qotd_channels = AsyncMock(return_value=[{"channel_id": 2001}])

    modal = AddQotdChannelModal(qotd, guild_id=123, lang="en")
    modal.channel_select._values = ["2001"]
    modal.schedule_input._value = "14:30"
    role_mock = MagicMock()
    role_mock.id = 3001
    modal.role_select._values = [role_mock]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.message = MagicMock()
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.edit_message = AsyncMock()

    await modal.on_submit(inter)
    qotd.add_qotd_channel.assert_awaited_once()
    call_args, call_kwargs = qotd.add_qotd_channel.call_args
    assert call_args[0] == 123
    assert call_args[1] == 2001
    assert call_kwargs["role_id"] == 3001
    assert call_kwargs["scheduled_time"] == "14:30"

    inter.response.edit_message.assert_awaited_once()
    edit_kwargs = inter.response.edit_message.call_args[1]
    assert "<#2001>" in edit_kwargs["content"]


@pytest.mark.asyncio
async def test_on_add_channel_click_sends_modal():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    view = QotdPanelView(qotd, section="settings", guild_id=123, channels=[])

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.send_modal = AsyncMock()

    await view.on_add_channel_click(inter)
    inter.response.send_modal.assert_awaited_once()
    modal_arg = inter.response.send_modal.call_args[0][0]
    assert isinstance(modal_arg, AddQotdChannelModal)


@pytest.mark.asyncio
async def test_qotd_panel_view_settings_buttons():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    view = QotdPanelView(qotd, section="settings", guild_id=123, channels=[])

    button_labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]
    button_emojis = [str(item.emoji) for item in view.children if isinstance(item, discord.ui.Button)]

    assert "Add QOTD Channel" in button_labels
    assert "Settings" in button_labels
    assert str(Icon.SETTINGS) in button_emojis
    assert "Admin Review Channel" not in button_labels
    assert "Change Language" not in button_labels
    assert "Suggestion Role" not in button_labels


def test_guild_settings_modal_structure():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.get_server_language = MagicMock(return_value="cs")

    modal = GuildSettingsModal(
        qotd,
        guild_id=123,
        current_admin_channel_id=2001,
        current_language="cs",
        current_suggest_role_id=3001,
        lang="en",
    )
    assert modal.title == "Server Settings"
    assert len(modal.children) == 3
    assert isinstance(modal.children[0], discord.ui.Label)
    assert isinstance(modal.children[1], discord.ui.Label)
    assert isinstance(modal.children[2], discord.ui.Label)

    assert modal.admin_select.channel_types == [
        discord.ChannelType.text,
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
    ]
    assert modal.admin_select.default_values[0].id == 2001
    assert modal.role_select.default_values[0].id == 3001

    cs_opt = next(opt for opt in modal.lang_select.options if opt.value == "cs")
    assert cs_opt.default is True


@pytest.mark.asyncio
async def test_guild_settings_modal_admin_check():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = GuildSettingsModal(qotd, guild_id=123, lang="en")

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = False
    inter.user.guild_permissions.administrator = False
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "administrators" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_guild_settings_modal_success():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.set_guild_language = AsyncMock()
    qotd.update_guild_settings = AsyncMock()
    qotd.build_settings_embed = AsyncMock(return_value=MagicMock())
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    modal = GuildSettingsModal(
        qotd,
        guild_id=123,
        current_admin_channel_id=None,
        current_language="en",
        current_suggest_role_id=None,
        lang="en",
    )
    ch_mock = MagicMock()
    ch_mock.id = 2005
    modal.admin_select._values = [ch_mock]
    modal.lang_select._values = ["de"]
    role_mock = MagicMock()
    role_mock.id = 3005
    modal.role_select._values = [role_mock]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.message = MagicMock()
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.edit_message = AsyncMock()

    await modal.on_submit(inter)
    qotd.set_guild_language.assert_awaited_once_with(123, "de")
    qotd.update_guild_settings.assert_awaited_once_with(
        123,
        admin_channel_id=2005,
        suggest_role_id=3005,
    )
    inter.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_guild_settings_modal_clear_values():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.set_guild_language = AsyncMock()
    qotd.update_guild_settings = AsyncMock()
    qotd.build_settings_embed = AsyncMock(return_value=MagicMock())
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    modal = GuildSettingsModal(
        qotd,
        guild_id=123,
        current_admin_channel_id=2005,
        current_language="en",
        current_suggest_role_id=3005,
        lang="en",
    )
    modal.admin_select._values = []
    modal.lang_select._values = ["en"]
    modal.role_select._values = []

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.message = MagicMock()
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.edit_message = AsyncMock()

    await modal.on_submit(inter)
    qotd.set_guild_language.assert_not_awaited()
    qotd.update_guild_settings.assert_awaited_once_with(
        123,
        admin_channel_id=None,
        suggest_role_id=None,
    )


@pytest.mark.asyncio
async def test_on_settings_click_sends_modal():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.get_server_language = MagicMock(return_value="en")
    qotd.get_guild_settings = AsyncMock(return_value={"admin_channel_id": 2001, "suggest_role_id": 3001})
    view = QotdPanelView(qotd, section="settings", guild_id=123, channels=[])

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.send_modal = AsyncMock()

    await view.on_settings_click(inter)
    inter.response.send_modal.assert_awaited_once()
    modal_arg = inter.response.send_modal.call_args[0][0]
    assert isinstance(modal_arg, GuildSettingsModal)
    assert modal_arg.current_admin_channel_id == 2001
    assert modal_arg.current_suggest_role_id == 3001


def test_channel_settings_modal_structure():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    chan_mock = MagicMock()
    chan_mock.name = "lego"
    qotd.bot.get_channel = MagicMock(return_value=chan_mock)

    modal = ChannelSettingsModal(
        qotd,
        guild_id=123,
        channel_id=2001,
        current_time="18:52",
        current_role_id=3001,
        current_thresh=5,
        lang="en",
    )
    assert "Settings: #lego" in modal.title
    assert len(modal.children) == 3
    assert modal.schedule_input.default == "18:52"
    assert modal.schedule_input.min_length == 5
    assert modal.schedule_input.max_length == 5
    assert modal.role_select.default_values[0].id == 3001
    assert modal.threshold_input.default == "5"
    assert modal.threshold_input.min_length == 1
    assert modal.threshold_input.max_length == 2

    modal_text_labels = [c.text if isinstance(c, discord.ui.Label) else (c.label if isinstance(c, discord.ui.TextInput) else "") for c in modal.children]
    assert not any("max" in str(lbl).lower() for lbl in modal_text_labels)


@pytest.mark.asyncio
async def test_channel_settings_modal_admin_check():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = ChannelSettingsModal(qotd, guild_id=123, channel_id=2001, lang="en")

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = False
    inter.user.guild_permissions.administrator = False
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "administrators" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_channel_settings_modal_invalid_time():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = ChannelSettingsModal(qotd, guild_id=123, channel_id=2001, lang="en")
    modal.schedule_input._value = "99:99"

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "invalid time" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_channel_settings_modal_invalid_threshold():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = ChannelSettingsModal(qotd, guild_id=123, channel_id=2001, lang="en")
    modal.schedule_input._value = "12:34"
    modal.threshold_input._value = "99"

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "between 1 and 50" in inter.response.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_channel_settings_modal_success():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.update_qotd_channel = AsyncMock()
    qotd.build_settings_embed = AsyncMock(return_value=MagicMock())
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    modal = ChannelSettingsModal(qotd, guild_id=123, channel_id=2001, lang="en")
    modal.schedule_input._value = "15:45"
    role_mock = MagicMock()
    role_mock.id = 4444
    modal.role_select._values = [role_mock]
    modal.threshold_input._value = "10"

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.message = MagicMock()
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.edit_message = AsyncMock()

    await modal.on_submit(inter)
    qotd.update_qotd_channel.assert_awaited_once()
    call_kwargs = qotd.update_qotd_channel.call_args[1]
    assert call_kwargs["scheduled_time"] == "15:45"
    assert call_kwargs["role_id"] == 4444
    assert call_kwargs["low_queue_threshold"] == 10
    inter.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_settings_modal_clear_role():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.update_qotd_channel = AsyncMock()
    qotd.build_settings_embed = AsyncMock(return_value=MagicMock())
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    modal = ChannelSettingsModal(qotd, guild_id=123, channel_id=2001, lang="en")
    modal.schedule_input._value = "15:45"
    modal.role_select._values = []
    modal.threshold_input._value = "5"

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.message = MagicMock()
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.edit_message = AsyncMock()

    await modal.on_submit(inter)
    call_kwargs = qotd.update_qotd_channel.call_args[1]
    assert call_kwargs["role_id"] is None


@pytest.mark.asyncio
async def test_on_select_manage_channel_opens_modal_directly():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.get_qotd_channel = AsyncMock(return_value={
        "channel_id": 2001,
        "scheduled_time": "18:52",
        "role_id": 3001,
        "low_queue_threshold": 3,
    })
    chan_mock = MagicMock()
    chan_mock.name = "lego"
    qotd.bot.get_channel = MagicMock(return_value=chan_mock)

    view = QotdPanelView(qotd, section="settings", guild_id=123, channels=[{"channel_id": 2001}])

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.data = {"values": ["2001"]}
    inter.response = MagicMock()
    inter.response.send_modal = AsyncMock()

    await view.on_select_manage_channel(inter)
    inter.response.send_modal.assert_awaited_once()
    modal_arg = inter.response.send_modal.call_args[0][0]
    assert isinstance(modal_arg, ChannelSettingsModal)
    assert modal_arg.channel_id == 2001
    assert modal_arg.current_time == "18:52"
    assert modal_arg.current_thresh == 3


@pytest.mark.asyncio
async def test_qotd_panel_settings_has_unlink_button():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    view = QotdPanelView(qotd, section="settings", guild_id=123, channels=[{"channel_id": 2001}])

    button_labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]
    assert "Unlink Channel" in button_labels


@pytest.mark.asyncio
async def test_channel_manage_view_buttons():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    view = ChannelManageView(qotd, guild_id=123, channel_id=2001, lang="en")

    button_labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]
    button_emojis = [str(item.emoji) for item in view.children if isinstance(item, discord.ui.Button)]

    assert "Settings" in button_labels
    assert str(Icon.SETTINGS) in button_emojis
    assert "Unlink Channel" in button_labels
    assert "Back" in button_labels

    # Old separate buttons are removed
    assert not any("hour" in lbl.lower() for lbl in button_labels)
    assert not any("role" in lbl.lower() for lbl in button_labels)
    assert not any("threshold" in lbl.lower() for lbl in button_labels)
    assert not any("max" in lbl.lower() for lbl in button_labels)


@pytest.mark.asyncio
async def test_build_channel_manage_embed_removes_max_capacity():
    cog = Qotd.__new__(Qotd)
    cog.get_user_language = MagicMock(return_value="en")
    cog.get_qotd_channel = AsyncMock(return_value={
        "channel_id": 2001,
        "scheduled_time": "18:52",
        "role_id": 3001,
        "low_queue_threshold": 3,
        "max_queue_limit": 500,
        "last_posted_date": "2026-09-07",
    })
    chan_mock = MagicMock()
    chan_mock.name = "lego"
    cog.bot = MagicMock()
    cog.bot.get_channel = MagicMock(return_value=chan_mock)
    cog.get_queue_count = AsyncMock(return_value=151)
    cog.get_total_question_count = AsyncMock(return_value=157)

    embed = await cog.build_channel_manage_embed(123, 2001, lang="en")
    field_names = [f.name for f in embed.fields]
    assert not any("max" in name.lower() for name in field_names)
    assert not any("kapacita" in name.lower() for name in field_names)


def test_unlink_channel_modal_structure():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    chan_mock = MagicMock()
    chan_mock.name = "lego"
    qotd.bot.get_channel = MagicMock(return_value=chan_mock)

    modal = UnlinkChannelModal(
        qotd,
        guild_id=123,
        channels=[{"channel_id": 2001}, {"channel_id": 2002}],
        target_channel_id=2002,
        lang="en",
    )
    assert modal.title == "Unlink QOTD Channel"
    assert len(modal.children) == 3
    assert modal.delete_questions_cb.value is False
    assert modal.delete_questions_cb.required is True
    assert modal.delete_questions_cb.min_values == 1
    assert modal.delete_questions_cb.max_values == 1
    assert modal.delete_questions_label.description == "Warning: Questions in database will be permanently deleted!"
    assert modal.confirm_cb.value is False
    assert modal.confirm_cb.required is True
    assert modal.confirm_cb.min_values == 1
    assert modal.confirm_cb.max_values == 1
    # Verify default selection matches target_channel_id
    opt_2002 = next(opt for opt in modal.channel_select.options if opt.value == "2002")
    assert opt_2002.default is True


@pytest.mark.asyncio
async def test_unlink_channel_modal_admin_check():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = UnlinkChannelModal(qotd, guild_id=123, channels=[{"channel_id": 2001}], lang="en")

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = False
    inter.user.guild_permissions.administrator = False
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "administrators" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_unlink_channel_modal_delete_not_confirmed():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = UnlinkChannelModal(qotd, guild_id=123, channels=[{"channel_id": 2001}], lang="en")
    modal.delete_questions_cb._value = False
    modal.confirm_cb._value = True

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "acknowledge" in inter.response.send_message.call_args[0][0].lower() or "deletion" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_unlink_channel_modal_not_confirmed():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    modal = UnlinkChannelModal(qotd, guild_id=123, channels=[{"channel_id": 2001}], lang="en")
    modal.delete_questions_cb._value = True
    modal.confirm_cb._value = False

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "are you sure" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_unlink_channel_modal_success_delete_questions():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.delete_qotd_channel = AsyncMock()
    qotd.build_settings_embed = AsyncMock(return_value=MagicMock())
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    modal = UnlinkChannelModal(qotd, guild_id=123, channels=[{"channel_id": 2001}], lang="en")
    modal.channel_select._values = ["2001"]
    modal.delete_questions_cb._value = True
    modal.confirm_cb._value = True

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.message = MagicMock()
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.edit_message = AsyncMock()

    await modal.on_submit(inter)
    qotd.delete_qotd_channel.assert_awaited_once_with(2001, delete_questions=True)
    inter.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_unlink_channel_click_sends_modal():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    view = QotdPanelView(qotd, section="settings", guild_id=123, channels=[{"channel_id": 2001}])

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.response = MagicMock()
    inter.response.send_modal = AsyncMock()

    await view.on_unlink_channel_click(inter)
    inter.response.send_modal.assert_awaited_once()
    modal_arg = inter.response.send_modal.call_args[0][0]
    assert isinstance(modal_arg, UnlinkChannelModal)
    assert modal_arg.target_channel_id == view.channel_id


@pytest.mark.asyncio
async def test_unlink_channel_modal_raw_payload_delete_questions():
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.delete_qotd_channel = AsyncMock()
    qotd.build_settings_embed = AsyncMock(return_value=MagicMock())
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    modal = UnlinkChannelModal(qotd, guild_id=123, channels=[{"channel_id": 2001}], lang="en")
    modal.channel_select._values = ["2001"]

    inter = MagicMock(spec=discord.Interaction)
    inter.user = MagicMock(spec=discord.Member)
    inter.user.guild_permissions.manage_guild = True
    inter.message = MagicMock()
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.edit_message = AsyncMock()
    # Simulate Discord client returning raw interaction data
    inter.data = {
        "components": [
            {
                "type": 18,
                "component": {"type": 3, "custom_id": modal.channel_select.custom_id, "values": ["2001"]},
            },
            {
                "type": 18,
                "component": {"type": 22, "custom_id": modal.delete_questions_cb.custom_id, "values": ["delete"]},
            },
            {
                "type": 18,
                "component": {"type": 22, "custom_id": modal.confirm_cb.custom_id, "values": ["confirm"]},
            },
        ]
    }

    await modal.on_submit(inter)
    qotd.delete_qotd_channel.assert_awaited_once_with(2001, delete_questions=True)
    inter.response.edit_message.assert_awaited_once()





