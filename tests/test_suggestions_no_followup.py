from unittest.mock import AsyncMock, MagicMock
import pytest
import discord
from cogs.qotd.views import SuggestionReviewButton, QotdPanelView, SuggestionDetailView
from cogs.qotd.modals import EditSuggestionModal, RejectSuggestionModal


def make_admin_member():
    member = MagicMock(spec=discord.Member)
    member.guild_permissions.manage_guild = True
    return member


@pytest.mark.asyncio
async def test_suggestion_review_button_accept_no_followup():
    """Verify SuggestionReviewButton accept action does not call followup.send on success."""
    qotd_cog = MagicMock()
    qotd_cog.get_user_language = MagicMock(return_value="en")
    qotd_cog.accept_suggestion = AsyncMock(return_value=(True, "ok"))

    raw_btn = discord.ui.Button(custom_id="qotd:review:accept:1:sugg-1")
    button = SuggestionReviewButton(item=raw_btn, action="accept", guild_id=1, suggestion_id="sugg-1")

    interaction = MagicMock()
    interaction.client.get_cog = MagicMock(return_value=qotd_cog)
    interaction.user = make_admin_member()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await button.callback(interaction)

    interaction.response.defer.assert_awaited_once_with()
    interaction.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_qotd_panel_approve_all_no_followup():
    """Verify QotdPanelView.on_approve_all does not call followup.send on success."""
    qotd = MagicMock()
    qotd.get_queue_count = AsyncMock(return_value=0)
    qotd.get_total_question_count = AsyncMock(return_value=0)
    qotd.accept_suggestion = AsyncMock(return_value=(True, "ok"))

    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[{"id": "sugg-1"}, {"id": "sugg-2"}])
    qotd.db.execute = AsyncMock(return_value=cursor)

    view = QotdPanelView.__new__(QotdPanelView)
    view.qotd = qotd
    view.guild_id = 1
    view.lang = "en"
    view.on_refresh = AsyncMock()

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await view.on_approve_all(interaction)

    interaction.response.defer.assert_awaited_once_with()
    interaction.followup.send.assert_not_called()
    view.on_refresh.assert_awaited_once_with(interaction)


@pytest.mark.asyncio
async def test_suggestion_detail_approve_no_followup():
    """Verify SuggestionDetailView.on_approve does not call followup.send on success."""
    qotd = MagicMock()
    qotd.accept_suggestion = AsyncMock(return_value=(True, "ok"))

    view = SuggestionDetailView.__new__(SuggestionDetailView)
    view.qotd = qotd
    view.guild_id = 1
    view.suggestion_id = "sugg-1"
    view.suggestion_row = {"question": "Test question?"}
    view.lang = "en"
    view.on_back = AsyncMock()

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await view.on_approve(interaction)

    interaction.response.defer.assert_awaited_once_with()
    interaction.followup.send.assert_not_called()
    view.on_back.assert_awaited_once_with(interaction)


@pytest.mark.asyncio
async def test_edit_suggestion_modal_no_followup():
    """Verify EditSuggestionModal does not call followup.send on success."""
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.accept_suggestion = AsyncMock(return_value=(True, "ok"))
    qotd.suggestions_page_count = AsyncMock(return_value=1)
    qotd.build_suggestions_embed = AsyncMock(return_value=MagicMock())
    qotd.get_suggestions_page = AsyncMock(return_value=[])
    qotd.get_qotd_channels = AsyncMock(return_value=[])

    # Test from_panel=True
    modal_panel = EditSuggestionModal(qotd, guild_id=1, suggestion_id="sugg-1", current_text="Old", from_panel=True)
    modal_panel.question_input._value = "New question text"

    interaction = MagicMock()
    interaction.user = make_admin_member()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal_panel.on_submit(interaction)

    interaction.response.defer.assert_awaited_once_with()
    interaction.followup.send.assert_not_called()

    # Test from_panel=False (review message)
    modal_card = EditSuggestionModal(qotd, guild_id=1, suggestion_id="sugg-1", current_text="Old", from_panel=False)
    modal_card.question_input._value = "New question text"

    interaction2 = MagicMock()
    interaction2.user = make_admin_member()
    interaction2.response.defer = AsyncMock()
    interaction2.followup.send = AsyncMock()

    await modal_card.on_submit(interaction2)

    interaction2.response.defer.assert_awaited_once_with()
    interaction2.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_reject_suggestion_modal_no_followup():
    """Verify RejectSuggestionModal does not call followup.send on success."""
    qotd = MagicMock()
    qotd.get_user_language = MagicMock(return_value="en")
    qotd.reject_suggestion = AsyncMock(return_value=True)

    # Test from_panel=False (review card)
    modal_card = RejectSuggestionModal(qotd, guild_id=1, suggestion_id="sugg-1", from_panel=False)
    modal_card.reason_input._value = "Duplicate question"

    interaction = MagicMock()
    interaction.user = make_admin_member()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal_card.on_submit(interaction)

    interaction.response.defer.assert_awaited_once_with()
    interaction.followup.send.assert_not_called()
