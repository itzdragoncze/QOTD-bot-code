import inspect
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union, TYPE_CHECKING
if TYPE_CHECKING:
    from .cog import Qotd
import discord
from translations import t, DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
from .constants import (
    logger,
    COLOR_QUEUE,
    COLOR_SUGGESTIONS,
    COLOR_HISTORY,
    COLOR_SETTINGS,
    COLOR_LEADERBOARD,
    COLOR_DETAIL,
    COLOR_DANGER,
    COLOR_POST,
    DEFAULT_SCHEDULED_TIME,
    REVIEW_CUSTOM_ID,
    MAX_QUEUE_QUESTIONS,
    MAX_TOTAL_QUESTIONS,
    format_discord_timestamp,
    is_admin,
    Icon,
)
from .modals import (
    SuggestionModal,
    AddQuestionModal,
    EditQuestionModal,
    EditSuggestionModal,
    RejectSuggestionModal,
    SearchQuestionsModal,
    QotdHourModal,
    QotdThresholdModal,
    AddQotdChannelModal,
    GuildSettingsModal,
    ChannelSettingsModal,
    UnlinkChannelModal,
)

class BaseTimeoutView(discord.ui.View):
    """Base view that disables all interactive components when the view times out,
    preventing 'This interaction failed' errors on expired buttons/selects."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_message: Optional[discord.InteractionMessage] = None

    def set_message(self, message: discord.InteractionMessage) -> None:
        """Store reference to the message so on_timeout can edit it."""
        self._original_message = message

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self._original_message:
            try:
                await self._original_message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class SuggestionReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=REVIEW_CUSTOM_ID):
    def __init__(self, item: discord.ui.Button, action: str, guild_id: int, suggestion_id: str):
        super().__init__(item)
        self.action = action
        self.guild_id = guild_id
        self.suggestion_id = suggestion_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(item, match.group(1), int(match.group(2)), match.group(3))

    async def callback(self, interaction: discord.Interaction):
        qotd_cog: Optional["Qotd"] = interaction.client.get_cog("Qotd")
        if not qotd_cog:
            await interaction.response.send_message("QOTD system unavailable.", ephemeral=True)
            return

        admin_lang = qotd_cog.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(admin_lang, "admin_only"), ephemeral=True)
            return

        if self.action == "accept":
            await interaction.response.defer()
            ok, reason = await qotd_cog.accept_suggestion(
                self.guild_id,
                self.suggestion_id,
                review_message=interaction.message,
                added_by=interaction.user,
                interaction=interaction,
            )
            if not ok:
                if reason == "queue_full":
                    ch_limit = MAX_QUEUE_QUESTIONS
                    cursor = await qotd_cog.db.execute("SELECT target_channel_id FROM suggestions WHERE id = ?", (self.suggestion_id,))
                    s_row = await cursor.fetchone()
                    if s_row and s_row[0]:
                        ch_row = await qotd_cog.get_qotd_channel(s_row[0])
                        if ch_row and ch_row.get("max_queue_limit") is not None:
                            ch_limit = ch_row["max_queue_limit"]
                    await interaction.followup.send(t(admin_lang, "sugg_cannot_approve_queue", max_q=ch_limit), ephemeral=True)
                elif reason == "total_limit":
                    await interaction.followup.send(t(admin_lang, "sugg_cannot_approve_total", max_total=MAX_TOTAL_QUESTIONS), ephemeral=True)
                else:
                    await interaction.followup.send(t(admin_lang, "sugg_approve_failed"), ephemeral=True)

        elif self.action == "decline":
            modal = RejectSuggestionModal(qotd_cog, self.guild_id, self.suggestion_id, lang=admin_lang)
            await interaction.response.send_modal(modal)

        elif self.action == "edit":
            cursor = await qotd_cog.db.execute(
                "SELECT question, target_channel_id FROM suggestions WHERE id = ? AND guild_id = ?",
                (self.suggestion_id, self.guild_id),
            )
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message(t(admin_lang, "not_found"), ephemeral=True)
                return
            channels = await qotd_cog.get_qotd_channels(self.guild_id)
            modal = EditSuggestionModal(
                qotd_cog,
                self.guild_id,
                self.suggestion_id,
                row["question"],
                channels=channels,
                target_channel_id=row["target_channel_id"],
                lang=admin_lang,
            )
            await interaction.response.send_modal(modal)


def suggestion_review_view(guild_id: int, suggestion_id: str, lang: str = "en") -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    b_acc = discord.ui.Button(
        label=t(lang, "btn_approve"),
        style=discord.ButtonStyle.success,
        emoji=Icon.CHECK,
        custom_id=f"qotd:review:accept:{guild_id}:{suggestion_id}",
    )
    b_dec = discord.ui.Button(
        label=t(lang, "btn_decline"),
        style=discord.ButtonStyle.danger,
        emoji=Icon.CANCEL,
        custom_id=f"qotd:review:decline:{guild_id}:{suggestion_id}",
    )
    b_edit = discord.ui.Button(
        label=t(lang, "btn_edit_approve"),
        style=discord.ButtonStyle.secondary,
        emoji=Icon.EDIT,
        custom_id=f"qotd:review:edit:{guild_id}:{suggestion_id}",
    )
    v.add_item(b_acc)
    v.add_item(b_edit)
    v.add_item(b_dec)
    return v


# ---------------------------------------------------------------------------
# Persistent Post Buttons & Suggestion Views
# ---------------------------------------------------------------------------

class QotdView(discord.ui.View):
    def __init__(self, qotd: "Qotd", lang: str = "en"):
        super().__init__(timeout=None)
        self.qotd = qotd
        self.lang = lang
        self.suggest_btn.label = t(lang, "btn_suggest")
        self.info_btn.label = t(lang, "btn_info")

    @discord.ui.button(
        label="Suggest Question",
        style=discord.ButtonStyle.secondary,
        emoji=Icon.BULB,
        custom_id="qotd:suggest",
    )
    async def suggest_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id is None:
            await interaction.response.send_message("This button can only be used in a server.", ephemeral=True)
            return
        user_lang = self.qotd.get_user_language(interaction, interaction.guild_id)
        channels = await self.qotd.get_qotd_channels(interaction.guild_id)

        if not channels:
            await interaction.response.send_message(t(user_lang, "no_channels_configured"), ephemeral=True)
            return

        settings = await self.qotd.get_guild_settings(interaction.guild_id)
        suggest_role_id = settings.get("suggest_role_id")
        if suggest_role_id:
            is_allowed = False
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            if member:
                if getattr(member.guild_permissions, "administrator", False) or getattr(member.guild_permissions, "manage_guild", False):
                    is_allowed = True
                elif any(r.id == suggest_role_id for r in member.roles):
                    is_allowed = True
            if not is_allowed:
                await interaction.response.send_message(t(user_lang, "sugg_role_required", role_id=suggest_role_id), ephemeral=True)
                return

        active_ch = interaction.channel_id if interaction.channel_id in [c["channel_id"] for c in channels] else None
        modal = SuggestionModal(
            self.qotd,
            interaction.guild_id,
            channels=channels,
            target_channel_id=active_ch,
            lang=user_lang,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Info",
        style=discord.ButtonStyle.secondary,
        emoji=Icon.INFO,
        custom_id="qotd:info",
    )
    async def info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_lang = self.qotd.get_user_language(interaction, interaction.guild_id)
        embed = await self.qotd.build_info_embed(interaction.guild_id, lang=user_lang)
        await interaction.followup.send(embed=embed, ephemeral=True)


class SuggestionChannelSelectView(BaseTimeoutView):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        channels: List[Dict[str, Any]],
        pending_question: Optional[str] = None,
        pending_note: Optional[str] = None,
        lang: Optional[str] = None,
    ):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.channels = channels
        self.pending_question = pending_question
        self.pending_note = pending_note
        self.lang = lang or qotd.get_user_language(None, guild_id)

        options = []
        for ch in channels[:25]:
            ch_id = ch["channel_id"]
            discord_ch = qotd.bot.get_channel(ch_id)
            ch_name = discord_ch.name if discord_ch else str(ch_id)
            options.append(discord.SelectOption(label=f"#{ch_name}", value=str(ch_id)))

        select = discord.ui.Select(
            placeholder=t(self.lang, "select_channel_suggest_placeholder"),
            options=options,
            row=0,
        )
        select.callback = self.on_select_channel
        self.add_item(select)

    async def on_select_channel(self, interaction: discord.Interaction):
        selected_id = int(interaction.data["values"][0])
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if self.pending_question:
            await interaction.response.defer(ephemeral=True)
            ok, msg = await self.qotd.handle_user_suggestion(
                guild_id=self.guild_id,
                user=interaction.user,
                question_text=self.pending_question,
                note=self.pending_note,
                user_locale=getattr(interaction, "locale", None),
                lang=user_lang,
                target_channel_id=selected_id,
            )
            await interaction.edit_original_response(content=msg, embed=None, view=None)
        else:
            modal = SuggestionModal(
                self.qotd,
                self.guild_id,
                channels=self.channels,
                target_channel_id=selected_id,
                lang=user_lang,
            )
            await interaction.response.send_modal(modal)


# ---------------------------------------------------------------------------
# Navigation & Selection Components
# ---------------------------------------------------------------------------

class QotdMenuSelect(discord.ui.Select):
    def __init__(self, qotd: "Qotd", selected: str = "to_ask", channel_id: Optional[int] = None, lang: str = "en"):
        options = [
            discord.SelectOption(
                label=t(lang, "sec_to_ask"),
                value="to_ask",
                emoji=Icon.FORUM,
                default=(selected == "to_ask"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_suggestions"),
                value="suggestions",
                emoji=Icon.BULB,
                default=(selected == "suggestions"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_asked"),
                value="asked",
                emoji=Icon.CALENDAR,
                default=(selected == "asked"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_settings"),
                value="settings",
                emoji=Icon.SETTINGS,
                default=(selected == "settings"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_top"),
                value="top",
                emoji=Icon.TROPHY,
                default=(selected == "top"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_info"),
                value="info",
                emoji=Icon.INFO,
                default=(selected == "info"),
            ),
        ]
        super().__init__(placeholder=t(lang, "nav_menu_placeholder"), min_values=1, max_values=1, options=options, row=0)
        self.qotd = qotd
        self.channel_id = channel_id
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        await self.qotd.show_menu_section(interaction, self.values[0], channel_id=self.channel_id, lang=self.lang)


# ---------------------------------------------------------------------------
# Panel View (Unified Control Center)
# ---------------------------------------------------------------------------

class QotdPanelView(BaseTimeoutView):
    def __init__(
        self,
        qotd: "Qotd",
        section: str = "to_ask",
        page: int = 0,
        guild_id: Optional[int] = None,
        total_pages: int = 1,
        page_items: Optional[List[Any]] = None,
        channel_id: Optional[int] = None,
        channels: Optional[List[Dict[str, Any]]] = None,
        lang: Optional[str] = None,
    ):
        super().__init__(timeout=300)
        self.qotd = qotd
        self.section = section
        self.page = page
        self.guild_id = guild_id
        self.total_pages = max(1, total_pages)
        self.page_items = page_items or []
        self.channel_id = channel_id
        self.channels = channels or []
        self.lang = lang or (qotd.get_user_language(None, guild_id) if guild_id else DEFAULT_LANGUAGE)

        # Row 0: Navigation Menu
        self.add_item(QotdMenuSelect(qotd, section, channel_id=self.channel_id, lang=self.lang))

        # Row 1: Channel Select Dropdown (for Queue & History) or Manage Dropdown (for Settings)
        if section in ("to_ask", "asked") and self.channels:
            ch_options = []
            for c in self.channels[:25]:
                cid = c["channel_id"]
                ch_obj = qotd.bot.get_channel(cid)
                c_name = ch_obj.name if ch_obj else str(cid)
                ch_options.append(
                    discord.SelectOption(
                        label=f"#{c_name}",
                        value=str(cid),
                        default=(cid == self.channel_id),
                    )
                )
            ch_placeholder = t(self.lang, "channel_select_queue_placeholder") if section == "to_ask" else t(self.lang, "channel_select_history_placeholder")
            ch_select = discord.ui.Select(placeholder=ch_placeholder, options=ch_options, row=1)
            ch_select.callback = self.on_channel_change
            self.add_item(ch_select)

        elif section == "settings" and self.channels:
            manage_options = []
            for c in self.channels[:25]:
                cid = c["channel_id"]
                ch_obj = qotd.bot.get_channel(cid)
                c_name = ch_obj.name if ch_obj else str(cid)
                sched = c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME
                manage_options.append(
                    discord.SelectOption(
                        label=f"#{c_name}",
                        value=str(cid),
                        description=f"Time: {sched} • ID: {cid}",
                    )
                )
            manage_select = discord.ui.Select(
                placeholder=t(self.lang, "select_channel_manage_placeholder"),
                options=manage_options,
                row=1,
            )
            manage_select.callback = self.on_select_manage_channel
            self.add_item(manage_select)

        # Row 2: Item Select Dropdown (for questions or suggestions)
        if section in ("to_ask", "asked") and self.page_items:
            options = []
            for item in self.page_items[:25]:
                q_id = item["id"]
                q_text = item["question"]
                author = item["suggested_by_name"] or item["added_by_name"] or t(self.lang, "source_system")
                label = f"#{q_id} • {q_text[:70]}"
                by_prefix = t(self.lang, "author_prefix")
                desc = f"{by_prefix}: {author[:30]} | {item['created_at'][:10]}"
                options.append(discord.SelectOption(label=label, value=str(q_id), description=desc))

            select_placeholder = t(self.lang, "select_q_placeholder") if section == "to_ask" else t(self.lang, "select_history_placeholder")
            item_select = discord.ui.Select(placeholder=select_placeholder, options=options, row=2)
            item_select.callback = self.on_question_selected
            self.add_item(item_select)

        elif section == "suggestions" and self.page_items:
            options = []
            for item in self.page_items[:25]:
                s_id = item["id"]
                s_text = item["question"]
                author = item["user_name"]
                label = f"{s_text[:75]}"
                by_prefix = t(self.lang, "suggested_by_prefix")
                desc = f"{by_prefix}: {author[:35]} | {item['created_at'][:10]}"
                options.append(discord.SelectOption(label=label, value=str(s_id), description=desc))

            sugg_placeholder = t(self.lang, "select_sugg_placeholder")
            sugg_select = discord.ui.Select(placeholder=sugg_placeholder, options=options, row=1)
            sugg_select.callback = self.on_suggestion_selected
            self.add_item(sugg_select)

        # Row 3: Action Buttons
        if section == "to_ask":
            btn_add = discord.ui.Button(label=t(self.lang, "btn_add_questions"), style=discord.ButtonStyle.success, emoji=Icon.ADD_NOTES, row=3)
            btn_add.callback = self.on_add_questions
            self.add_item(btn_add)

            btn_search = discord.ui.Button(label=t(self.lang, "btn_search"), style=discord.ButtonStyle.secondary, emoji=Icon.SEARCH, row=3)
            btn_search.callback = self.on_search
            self.add_item(btn_search)

            if self.page_items:
                btn_remove = discord.ui.Button(label=t(self.lang, "btn_bulk_delete"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=3)
                btn_remove.callback = self.on_bulk_delete
                self.add_item(btn_remove)

                btn_send = discord.ui.Button(label=t(self.lang, "btn_send_random"), style=discord.ButtonStyle.primary, emoji=Icon.SHUFFLE, row=3)
                btn_send.callback = self.on_send_random
                self.add_item(btn_send)

        elif section == "asked":
            btn_search = discord.ui.Button(label=t(self.lang, "btn_search"), style=discord.ButtonStyle.secondary, emoji=Icon.SEARCH, row=3)
            btn_search.callback = self.on_search
            self.add_item(btn_search)

            if self.page_items:
                btn_remove = discord.ui.Button(label=t(self.lang, "btn_bulk_delete"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=3)
                btn_remove.callback = self.on_bulk_delete
                self.add_item(btn_remove)

                btn_clear = discord.ui.Button(label=t(self.lang, "btn_clear_category"), style=discord.ButtonStyle.secondary, emoji=Icon.DELETE_FOREVER, row=3)
                btn_clear.callback = self.on_clear_category
                self.add_item(btn_clear)

        elif section == "suggestions" and self.page_items:
            btn_approve_all = discord.ui.Button(label=t(self.lang, "btn_approve_all"), style=discord.ButtonStyle.success, emoji=Icon.CHECK, row=2)
            btn_approve_all.callback = self.on_approve_all
            self.add_item(btn_approve_all)

            btn_clear_sugg = discord.ui.Button(label=t(self.lang, "btn_clear_category"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=2)
            btn_clear_sugg.callback = self.on_clear_category
            self.add_item(btn_clear_sugg)

        elif section == "settings":
            btn_add_chan = discord.ui.Button(label=t(self.lang, "btn_add_qotd_channel"), style=discord.ButtonStyle.success, emoji=Icon.ADD_BOX, row=2)
            btn_add_chan.callback = self.on_add_channel_click
            self.add_item(btn_add_chan)

            btn_settings = discord.ui.Button(label=t(self.lang, "btn_settings"), style=discord.ButtonStyle.secondary, emoji=Icon.SETTINGS, row=2)
            btn_settings.callback = self.on_settings_click
            self.add_item(btn_settings)

            if self.channels:
                btn_unlink = discord.ui.Button(label=t(self.lang, "btn_unlink_channel"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=2)
                btn_unlink.callback = self.on_unlink_channel_click
                self.add_item(btn_unlink)

        # Row 4: Pagination & Refresh Controls
        btn_row = 4 if section in ("to_ask", "asked") else 3
        if section in ("to_ask", "asked", "suggestions") and self.total_pages > 1:
            btn_first = discord.ui.Button(emoji=Icon.FIRST_PAGE, style=discord.ButtonStyle.secondary, disabled=(self.page == 0), row=btn_row)
            btn_first.callback = self.on_page_first
            self.add_item(btn_first)

            btn_prev = discord.ui.Button(emoji=Icon.ARROW_LEFT, style=discord.ButtonStyle.primary, disabled=(self.page == 0), row=btn_row)
            btn_prev.callback = self.on_page_prev
            self.add_item(btn_prev)

            btn_next = discord.ui.Button(emoji=Icon.ARROW_RIGHT, style=discord.ButtonStyle.primary, disabled=(self.page >= self.total_pages - 1), row=btn_row)
            btn_next.callback = self.on_page_next
            self.add_item(btn_next)

            btn_last = discord.ui.Button(emoji=Icon.LAST_PAGE, style=discord.ButtonStyle.secondary, disabled=(self.page >= self.total_pages - 1), row=btn_row)
            btn_last.callback = self.on_page_last
            self.add_item(btn_last)

        btn_refresh = discord.ui.Button(label=t(self.lang, "btn_refresh"), style=discord.ButtonStyle.secondary, emoji=Icon.AUTORENEW, row=btn_row)
        btn_refresh.callback = self.on_refresh
        self.add_item(btn_refresh)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_channel_change(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_channel_id = int(interaction.data["values"][0])
        await self.qotd.show_menu_section(interaction, self.section, channel_id=new_channel_id, lang=self.lang)

    async def on_select_manage_channel(self, interaction: discord.Interaction):
        selected_id = int(interaction.data["values"][0])
        c = await self.qotd.get_qotd_channel(selected_id)
        cur_time = (c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME) if c else DEFAULT_SCHEDULED_TIME
        cur_role = c.get("role_id") if c else None
        cur_thresh = (c.get("low_queue_threshold") or 3) if c else 3
        modal = ChannelSettingsModal(
            self.qotd,
            self.guild_id,
            selected_id,
            current_time=cur_time,
            current_role_id=cur_role,
            current_thresh=cur_thresh,
            lang=self.lang,
        )
        await interaction.response.send_modal(modal)

    async def on_unlink_channel_click(self, interaction: discord.Interaction):
        if not self.channels:
            return
        modal = UnlinkChannelModal(self.qotd, self.guild_id, channels=self.channels, target_channel_id=self.channel_id, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_add_channel_click(self, interaction: discord.Interaction):
        modal = AddQotdChannelModal(self.qotd, self.guild_id, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_settings_click(self, interaction: discord.Interaction):
        guild_id = self.guild_id or (interaction.guild.id if interaction.guild else 0)
        settings = await self.qotd.get_guild_settings(guild_id)
        current_admin_id = settings.get("admin_channel_id") if settings else None
        current_lang = self.qotd.get_server_language(guild_id)
        current_role_id = settings.get("suggest_role_id") if settings else None
        modal = GuildSettingsModal(
            self.qotd,
            guild_id,
            current_admin_channel_id=current_admin_id,
            current_language=current_lang,
            current_suggest_role_id=current_role_id,
            lang=self.lang,
        )
        await interaction.response.send_modal(modal)

    async def on_admin_channel_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = AdminChannelSelectView(self.qotd, self.guild_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "settings_admin_channel"),
            description="Select a text channel or thread for administrators to review member suggestions and receive queue warnings.",
            colour=COLOR_SETTINGS,
        )
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_question_selected(self, interaction: discord.Interaction):
        await interaction.response.defer()
        q_id = int(interaction.data["values"][0])
        q_row = await self.qotd.get_question(self.guild_id, q_id)
        if not q_row:
            await interaction.followup.send(t(self.lang, "not_found"), ephemeral=True)
            return

        embed = await self.qotd.build_question_detail_embed(self.guild_id, q_row, lang=self.lang)
        view = QuestionDetailView(self.qotd, self.guild_id, q_row, return_section=self.section, return_page=self.page, channel_id=self.channel_id, lang=self.lang)
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_suggestion_selected(self, interaction: discord.Interaction):
        await interaction.response.defer()
        s_id = interaction.data["values"][0]
        s_row = await self.qotd.get_suggestion(self.guild_id, s_id)
        if not s_row:
            await interaction.followup.send(t(self.lang, "not_found"), ephemeral=True)
            return

        embed = await self.qotd.build_suggestion_detail_embed(self.guild_id, s_row, lang=self.lang)
        view = SuggestionDetailView(self.qotd, self.guild_id, s_row, return_page=self.page, lang=self.lang)
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_add_questions(self, interaction: discord.Interaction):
        channels = getattr(self, "channels", None) or await self.qotd.get_qotd_channels(self.guild_id)
        if not channels:
            await interaction.response.send_message(t(self.lang, "no_channels_configured"), ephemeral=True)
            return
        target_ch = self.channel_id
        if target_ch not in [c["channel_id"] for c in channels]:
            target_ch = channels[0]["channel_id"]
        modal = AddQuestionModal(self.qotd, self.guild_id, current_page=self.page, channel_id=target_ch, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_search(self, interaction: discord.Interaction):
        modal = SearchQuestionsModal(self.qotd, self.guild_id, status_filter=self.section, channel_id=self.channel_id, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_bulk_delete(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = BulkDeleteSelectView(self.qotd, self.guild_id, self.section, self.page, self.page_items, channel_id=self.channel_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "bulk_delete_title"),
            description=t(self.lang, "bulk_delete_desc"),
            colour=COLOR_DANGER,
        )
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_clear_category(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = ClearQuestionsConfirmView(self.qotd, self.guild_id, self.section, self.page, channel_id=self.channel_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "clear_category_title", section=self.section),
            description=t(self.lang, "clear_category_desc", section=self.section),
            colour=COLOR_DANGER,
        )
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_approve_all(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channels = getattr(self, "channels", None)
        if channels is None:
            if hasattr(self.qotd, "get_qotd_channels"):
                res = self.qotd.get_qotd_channels(self.guild_id)
                channels = (await res) if inspect.isawaitable(res) else (res if isinstance(res, list) else [])
            else:
                channels = []
        cursor = await self.qotd.db.execute("SELECT * FROM suggestions WHERE guild_id = ? ORDER BY created_at ASC", (self.guild_id,))
        all_suggs = await cursor.fetchall()
        if not all_suggs:
            await interaction.followup.send(t(self.lang, "no_pending_suggestions"), ephemeral=True)
            return

        approved_count = 0
        skipped_queue_count = 0
        skipped_total_count = 0

        for s in all_suggs:
            ok, reason = await self.qotd.accept_suggestion(self.guild_id, s["id"], added_by=interaction.user)
            if ok:
                approved_count += 1
            elif reason == "queue_full":
                skipped_queue_count += 1
            elif reason == "total_limit":
                skipped_total_count += 1
                break

        if skipped_queue_count > 0 or skipped_total_count > 0:
            msg_parts = []
            if approved_count > 0:
                msg_parts.append(f"✅ Approved **{approved_count}** suggestion(s).")
            if skipped_queue_count > 0:
                msg_parts.append(f"⚠️ Skipped **{skipped_queue_count}** suggestion(s) due to channel queue capacity.")
            if skipped_total_count > 0:
                msg_parts.append(f"⚠️ Skipped **{skipped_total_count}** suggestion(s) due to server total limit ({MAX_TOTAL_QUESTIONS}).")
            await interaction.followup.send("\n".join(msg_parts), ephemeral=True)
        elif approved_count == 0:
            await interaction.followup.send(t(self.lang, "sugg_approve_failed"), ephemeral=True)

        await self.on_refresh(interaction)

    async def on_send_random(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.channel_id:
            await interaction.followup.send("⚠️ Please select a channel first.", ephemeral=True)
            return
        cursor = await self.qotd.db.execute(
            "SELECT id FROM questions WHERE guild_id = ? AND channel_id = ? AND status = 'to_ask' ORDER BY RANDOM() LIMIT 1",
            (self.guild_id, self.channel_id),
        )
        row = await cursor.fetchone()
        if not row:
            await interaction.followup.send(t(self.lang, "queue_empty"), ephemeral=True)
            return
        if await self.qotd.post_question(self.guild_id, self.channel_id, question_id=row[0]):
            await interaction.followup.send(t(self.lang, "qotd_send_success"), ephemeral=True)
            await self.on_refresh(interaction)
        else:
            await interaction.followup.send(t(self.lang, "qotd_send_error"), ephemeral=True)

    async def on_change_language(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = LanguageSelectView(self.qotd, self.guild_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "change_lang_title"),
            description=t(self.lang, "change_lang_desc"),
            colour=COLOR_SETTINGS,
        )
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_suggest_role_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        settings = await self.qotd.get_guild_settings(self.guild_id)
        current_role_id = settings.get("suggest_role_id")
        view = SuggestRoleSelectView(self.qotd, self.guild_id, current_role_id=current_role_id, lang=self.lang)
        embed = discord.Embed(
            title=f"{Icon.BULB} " + t(self.lang, "settings_suggest_role"),
            description=t(self.lang, "settings_suggest_role_desc"),
            colour=COLOR_SETTINGS,
        )
        if current_role_id:
            embed.add_field(name=t(self.lang, "current_role_label"), value=f"<@&{current_role_id}>", inline=False)
        else:
            embed.add_field(name=t(self.lang, "current_role_label"), value=t(self.lang, "settings_everyone"), inline=False)
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_page_first(self, interaction: discord.Interaction):
        await self._navigate_page(interaction, 0)

    async def on_page_prev(self, interaction: discord.Interaction):
        await self._navigate_page(interaction, self.page - 1)

    async def on_page_next(self, interaction: discord.Interaction):
        await self._navigate_page(interaction, self.page + 1)

    async def on_page_last(self, interaction: discord.Interaction):
        await self._navigate_page(interaction, self.total_pages - 1)

    async def on_refresh(self, interaction: discord.Interaction):
        await self._navigate_page(interaction, self.page)

    async def _navigate_page(self, interaction: discord.Interaction, target_page: int):
        await interaction.response.defer()
        if self.section in ("to_ask", "asked"):
            total_pages = await self.qotd.question_page_count(self.guild_id, self.section, channel_id=self.channel_id)
            page = max(0, min(target_page, total_pages - 1))
            embed = await self.qotd.build_questions_embed(self.guild_id, self.section, page, channel_id=self.channel_id, lang=self.lang)
            page_items = await self.qotd.get_questions_page(self.guild_id, self.section, page, channel_id=self.channel_id)
            channels = await self.qotd.get_qotd_channels(self.guild_id)
            view = QotdPanelView(
                self.qotd,
                section=self.section,
                page=page,
                guild_id=self.guild_id,
                total_pages=total_pages,
                page_items=page_items,
                channel_id=self.channel_id,
                channels=channels,
                lang=self.lang,
            )
        elif self.section == "suggestions":
            total_pages = await self.qotd.suggestions_page_count(self.guild_id)
            page = max(0, min(target_page, total_pages - 1))
            embed = await self.qotd.build_suggestions_embed(self.guild_id, page, lang=self.lang)
            page_items = await self.qotd.get_suggestions_page(self.guild_id, page)
            view = QotdPanelView(self.qotd, section=self.section, page=page, guild_id=self.guild_id, total_pages=total_pages, page_items=page_items, lang=self.lang)
        elif self.section == "settings":
            embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
            channels = await self.qotd.get_qotd_channels(self.guild_id)
            view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        elif self.section == "top":
            embed = await self.qotd.build_top_embed(self.guild_id, lang=self.lang)
            view = QotdPanelView(self.qotd, section="top", guild_id=self.guild_id, lang=self.lang)
        else:
            embed = await self.qotd.build_info_embed(self.guild_id, lang=self.lang)
            view = QotdPanelView(self.qotd, section="info", guild_id=self.guild_id, lang=self.lang)

        await interaction.edit_original_response(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Language & Channel Management Views
# ---------------------------------------------------------------------------

class LanguageSelectView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        server_lang = qotd.get_server_language(guild_id)

        flags = {
            "en": "🇬🇧",
            "cs": "🇨🇿",
            "es": "🇪🇸",
            "pt": "🇵🇹",
            "sk": "🇸🇰",
            "de": "🇩🇪",
            "fr": "🇫🇷",
        }
        options = [
            discord.SelectOption(
                label=name,
                value=code,
                emoji=flags.get(code),
                default=(server_lang == code),
            )
            for code, name in SUPPORTED_LANGUAGES.items()
        ]
        placeholder = t(self.lang, "select_lang_placeholder")
        select_menu = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            row=0,
        )
        select_menu.callback = self.on_select_language
        self.add_item(select_menu)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_select_language(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_lang = interaction.data["values"][0]
        await self.qotd.set_guild_language(self.guild_id, new_lang)
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(
            content=t(self.lang, "lang_changed", language=SUPPORTED_LANGUAGES[new_lang]),
            embed=embed,
            view=view,
        )

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class AddQotdChannelSelectView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        self.chan_select = discord.ui.ChannelSelect(
            placeholder="Select text channel to add as QOTD channel...",
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        self.chan_select.callback = self.on_channel_selected
        self.add_item(self.chan_select)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_channel_selected(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channel_id = self.chan_select.values[0].id
        existing = await self.qotd.get_qotd_channel(channel_id)
        if existing:
            await interaction.followup.send(t(self.lang, "channel_already_exists"), ephemeral=True)
            return

        await self.qotd.add_qotd_channel(self.guild_id, channel_id)
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(
            content=t(self.lang, "channel_added", channel_id=channel_id),
            embed=embed,
            view=view,
        )

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class AdminChannelSelectView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        self.chan_select = discord.ui.ChannelSelect(
            placeholder="Select admin review channel...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.public_thread, discord.ChannelType.private_thread],
            row=0,
        )
        self.chan_select.callback = self.on_channel_selected
        self.add_item(self.chan_select)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_channel_selected(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channel_id = self.chan_select.values[0].id
        await self.qotd.update_guild_settings(self.guild_id, admin_channel_id=channel_id)
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(
            content=f"{Icon.CHECK} Admin review channel set to <#{channel_id}>.",
            embed=embed,
            view=view,
        )

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class ChannelManageView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, channel_id: int, lang: Optional[str] = None):
        super().__init__(timeout=300)
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        btn_settings = discord.ui.Button(label=t(self.lang, "btn_settings"), style=discord.ButtonStyle.secondary, emoji=Icon.SETTINGS, row=0)
        btn_settings.callback = self.on_settings_click
        self.add_item(btn_settings)

        btn_unlink = discord.ui.Button(label=t(self.lang, "btn_unlink_channel"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=0)
        btn_unlink.callback = self.on_unlink
        self.add_item(btn_unlink)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=0)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_settings_click(self, interaction: discord.Interaction):
        c = await self.qotd.get_qotd_channel(self.channel_id)
        cur_time = (c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME) if c else DEFAULT_SCHEDULED_TIME
        cur_role = c.get("role_id") if c else None
        cur_thresh = (c.get("low_queue_threshold") or 3) if c else 3
        modal = ChannelSettingsModal(
            self.qotd,
            self.guild_id,
            self.channel_id,
            current_time=cur_time,
            current_role_id=cur_role,
            current_thresh=cur_thresh,
            lang=self.lang,
        )
        await interaction.response.send_modal(modal)

    async def on_hour(self, interaction: discord.Interaction):
        c = await self.qotd.get_qotd_channel(self.channel_id)
        cur_time = (c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME) if c else DEFAULT_SCHEDULED_TIME
        modal = QotdHourModal(self.qotd, self.guild_id, self.channel_id, cur_time, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = ChannelRoleSelectView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "btn_manage_channel_role"),
            description=f"Select the subscriber role to mention when questions are published in <#{self.channel_id}>.",
            colour=COLOR_SETTINGS,
        )
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_threshold(self, interaction: discord.Interaction):
        c = await self.qotd.get_qotd_channel(self.channel_id)
        cur_thresh = (c.get("low_queue_threshold") or 3) if c else 3
        modal = QotdThresholdModal(self.qotd, self.guild_id, self.channel_id, cur_thresh, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_max_queue(self, interaction: discord.Interaction):
        c = await self.qotd.get_qotd_channel(self.channel_id)
        cur_max = (c.get("max_queue_limit") or 500) if c else 500
        from .modals import QotdMaxQueueModal
        modal = QotdMaxQueueModal(self.qotd, self.guild_id, self.channel_id, cur_max, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_unlink(self, interaction: discord.Interaction):
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        modal = UnlinkChannelModal(self.qotd, self.guild_id, channels=channels, target_channel_id=self.channel_id, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class ChannelRoleSelectView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, channel_id: int, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        self.role_select = discord.ui.RoleSelect(
            placeholder="Select subscriber mention role...",
            row=0,
        )
        self.role_select.callback = self.on_role_selected
        self.add_item(self.role_select)

        btn_clear = discord.ui.Button(label="Remove Role Mention", style=discord.ButtonStyle.danger, emoji=Icon.CANCEL, row=1)
        btn_clear.callback = self.on_clear_role
        self.add_item(btn_clear)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_role_selected(self, interaction: discord.Interaction):
        await interaction.response.defer()
        role_id = self.role_select.values[0].id
        await self.qotd.update_qotd_channel(self.channel_id, role_id=role_id)
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=self.lang)
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
        await interaction.edit_original_response(content=f"{Icon.CHECK} Role updated to <@&{role_id}>.", embed=embed, view=view)

    async def on_clear_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.qotd.update_qotd_channel(self.channel_id, role_id=None)
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=self.lang)
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
        await interaction.edit_original_response(content=f"{Icon.CHECK} Role mention removed.", embed=embed, view=view)

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=self.lang)
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class SuggestRoleSelectView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, current_role_id: Optional[int] = None, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.current_role_id = current_role_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        self.role_select = discord.ui.RoleSelect(
            placeholder=t(self.lang, "select_suggest_role_placeholder")[:100],
            row=0,
        )
        self.role_select.callback = self.on_role_selected
        self.add_item(self.role_select)

        btn_clear = discord.ui.Button(label=t(self.lang, "btn_clear_suggest_role"), style=discord.ButtonStyle.danger, emoji=Icon.CANCEL, row=1)
        btn_clear.callback = self.on_clear_role
        self.add_item(btn_clear)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_role_selected(self, interaction: discord.Interaction):
        await interaction.response.defer()
        role_id = self.role_select.values[0].id
        await self.qotd.update_guild_settings(self.guild_id, suggest_role_id=role_id)
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=f"{Icon.CHECK} " + t(self.lang, "suggest_role_updated", role=f"<@&{role_id}>"), embed=embed, view=view)

    async def on_clear_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.qotd.update_guild_settings(self.guild_id, suggest_role_id=None)
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=f"{Icon.CHECK} " + t(self.lang, "suggest_role_cleared"), embed=embed, view=view)

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class UnlinkChannelConfirmView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, channel_id: int, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        btn_keep = discord.ui.Button(label=t(self.lang, "btn_keep_questions"), style=discord.ButtonStyle.secondary, emoji=Icon.SAVE, row=0)
        btn_keep.callback = self.on_keep
        self.add_item(btn_keep)

        btn_del = discord.ui.Button(label=t(self.lang, "btn_delete_questions"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=0)
        btn_del.callback = self.on_delete
        self.add_item(btn_del)

        btn_cancel = discord.ui.Button(label=t(self.lang, "btn_cancel"), style=discord.ButtonStyle.secondary, emoji=Icon.CANCEL, row=0)
        btn_cancel.callback = self.on_cancel
        self.add_item(btn_cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_keep(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.qotd.delete_qotd_channel(self.channel_id, delete_questions=False)
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(
            content=t(self.lang, "channel_unlinked", channel_id=self.channel_id),
            embed=embed,
            view=view,
        )

    async def on_delete(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.qotd.delete_qotd_channel(self.channel_id, delete_questions=True)
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(
            content=t(self.lang, "channel_unlinked", channel_id=self.channel_id),
            embed=embed,
            view=view,
        )

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class UnlinkChannelSelectView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, channels: List[Dict[str, Any]], lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.channels = channels
        self.lang = lang or qotd.get_user_language(None, guild_id)

        options = []
        for c in channels[:25]:
            cid = c["channel_id"]
            ch_obj = qotd.bot.get_channel(cid)
            c_name = ch_obj.name if ch_obj else str(cid)
            options.append(discord.SelectOption(label=f"#{c_name}", value=str(cid)))

        select = discord.ui.Select(placeholder=t(self.lang, "select_channel_manage_placeholder")[:100], options=options, row=0)
        select.callback = self.on_select
        self.add_item(select)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channel_id = int(interaction.data["values"][0])
        view = UnlinkChannelConfirmView(self.qotd, self.guild_id, channel_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "unlink_confirm_title"),
            description=t(self.lang, "unlink_confirm_desc", channel_id=channel_id),
            colour=COLOR_DANGER,
        )
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=self.lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


# ---------------------------------------------------------------------------
# Detail Card Views (Question & Suggestion)
# ---------------------------------------------------------------------------

class QuestionDetailView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, question_row: Any, return_section: str = "to_ask", return_page: int = 0, channel_id: Optional[int] = None, lang: Optional[str] = None):
        super().__init__(timeout=300)
        self.qotd = qotd
        self.guild_id = guild_id
        self.question_row = question_row
        self.question_id = question_row["id"]
        self.status = question_row["status"]
        self.return_section = return_section
        self.return_page = return_page
        self.channel_id = channel_id or question_row.get("channel_id")
        self.lang = lang or qotd.get_user_language(None, guild_id)

        btn_edit = discord.ui.Button(label=t(self.lang, "btn_edit_text"), style=discord.ButtonStyle.primary, emoji=Icon.EDIT, row=0)
        btn_edit.callback = self.on_edit
        self.add_item(btn_edit)

        if self.status == "to_ask":
            btn_send = discord.ui.Button(label=t(self.lang, "btn_send_now"), style=discord.ButtonStyle.success, emoji=Icon.SEND, row=0)
            btn_send.callback = self.on_send_now
            self.add_item(btn_send)
        else:
            btn_requeue = discord.ui.Button(label=t(self.lang, "btn_requeue"), style=discord.ButtonStyle.success, emoji=Icon.AUTORENEW, row=0)
            btn_requeue.callback = self.on_requeue
            self.add_item(btn_requeue)

        btn_del = discord.ui.Button(label=t(self.lang, "btn_delete"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=0)
        btn_del.callback = self.on_delete
        self.add_item(btn_del)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=0)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_edit(self, interaction: discord.Interaction):
        modal = EditQuestionModal(
            self.qotd,
            self.guild_id,
            self.question_id,
            self.question_row["question"],
            return_section=self.return_section,
            return_page=self.return_page,
            channel_id=self.channel_id,
            lang=self.lang,
        )
        await interaction.response.send_modal(modal)

    async def on_send_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.channel_id:
            await interaction.followup.send("⚠️ No channel associated with this question.", ephemeral=True)
            return
        if await self.qotd.post_question(self.guild_id, self.channel_id, question_id=self.question_id):
            await interaction.followup.send(t(self.lang, "qotd_send_success"), ephemeral=True)
            await self.on_back(interaction)
        else:
            await interaction.followup.send(t(self.lang, "qotd_send_error"), ephemeral=True)

    async def on_requeue(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        effective_ch = self.channel_id or self.question_row.get("channel_id")
        if effective_ch is None:
            channels = await self.qotd.get_qotd_channels(self.guild_id)
            if len(channels) == 1:
                effective_ch = channels[0]["channel_id"]

        channel_max_limit = MAX_QUEUE_QUESTIONS
        if effective_ch:
            ch_row = await self.qotd.get_qotd_channel(effective_ch)
            if ch_row and ch_row.get("max_queue_limit") is not None:
                channel_max_limit = ch_row["max_queue_limit"]
        cur_queue = await self.qotd.get_queue_count(self.guild_id, effective_ch)
        if cur_queue >= channel_max_limit:
            await interaction.followup.send(t(self.lang, "requeue_queue_full", max_q=channel_max_limit), ephemeral=True)
            return
        if await self.qotd.requeue_question(self.guild_id, self.question_id, channel_id=effective_ch):
            await interaction.followup.send(t(self.lang, "requeue_success"), ephemeral=True)
            await self.on_back(interaction)
        else:
            await interaction.followup.send(t(self.lang, "requeue_error"), ephemeral=True)

    async def on_delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok = await self.qotd.delete_single_question(self.guild_id, self.question_id)
        if ok:
            await interaction.followup.send(t(self.lang, "delete_success", qid=self.question_id), ephemeral=True)
            await self.on_back(interaction)
        else:
            await interaction.followup.send(t(self.lang, "delete_error"), ephemeral=True)

    async def on_back(self, interaction: discord.Interaction):
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        embed = await self.qotd.build_questions_embed(self.guild_id, self.return_section, self.return_page, channel_id=self.channel_id, lang=self.lang)
        total_pages = await self.qotd.question_page_count(self.guild_id, self.return_section, channel_id=self.channel_id)
        page_items = await self.qotd.get_questions_page(self.guild_id, self.return_section, self.return_page, channel_id=self.channel_id)
        view = QotdPanelView(
            self.qotd,
            section=self.return_section,
            page=self.return_page,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channel_id=self.channel_id,
            channels=channels,
            lang=self.lang,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)


class SuggestionDeclinedView(BaseTimeoutView):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        return_page: int = 0,
        lang: Optional[str] = None,
    ):
        super().__init__(timeout=300)
        self.qotd = qotd
        self.guild_id = guild_id
        self.return_page = return_page
        self.lang = lang or qotd.get_user_language(None, guild_id)

        # Row 0: Section navigation menu so the user can switch anywhere
        self.add_item(QotdMenuSelect(qotd, selected="suggestions", lang=self.lang))

        # Row 1: Back to suggestions button so the user can navigate back
        btn_back = discord.ui.Button(
            label=t(self.lang, "btn_back_to_sugg"),
            style=discord.ButtonStyle.secondary,
            emoji=Icon.ARROW_LEFT,
            row=1,
        )
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        total_pages = await self.qotd.suggestions_page_count(self.guild_id)
        target_page = min(self.return_page, max(0, total_pages - 1))
        embed = await self.qotd.build_suggestions_embed(self.guild_id, target_page, lang=self.lang)
        page_items = await self.qotd.get_suggestions_page(self.guild_id, target_page)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(
            self.qotd,
            section="suggestions",
            page=target_page,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channels=channels,
            lang=self.lang,
        )
        await interaction.edit_original_response(embed=embed, view=view)


class SuggestionDetailView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, suggestion_row: Any, return_page: int = 0, lang: Optional[str] = None):
        super().__init__(timeout=300)
        self.qotd = qotd
        self.guild_id = guild_id
        self.suggestion_row = suggestion_row
        self.suggestion_id = suggestion_row["id"]
        self.return_page = return_page
        self.lang = lang or qotd.get_user_language(None, guild_id)

        btn_approve = discord.ui.Button(label=t(self.lang, "btn_approve"), style=discord.ButtonStyle.success, emoji=Icon.CHECK, row=0)
        btn_approve.callback = self.on_approve
        self.add_item(btn_approve)

        btn_edit = discord.ui.Button(label=t(self.lang, "btn_edit_approve"), style=discord.ButtonStyle.secondary, emoji=Icon.EDIT, row=0)
        btn_edit.callback = self.on_edit
        self.add_item(btn_edit)

        btn_decline = discord.ui.Button(label=t(self.lang, "btn_decline"), style=discord.ButtonStyle.danger, emoji=Icon.CANCEL, row=0)
        btn_decline.callback = self.on_decline
        self.add_item(btn_decline)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=0)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_approve(self, interaction: discord.Interaction):
        await interaction.response.defer()
        target_ch_id = self.suggestion_row.get("target_channel_id")
        ok, reason = await self.qotd.accept_suggestion(self.guild_id, self.suggestion_id, added_by=interaction.user, target_channel_id=target_ch_id)
        if ok:
            await self.on_back(interaction)
        else:
            if reason == "queue_full":
                ch_limit = MAX_QUEUE_QUESTIONS
                if target_ch_id:
                    ch_row = await self.qotd.get_qotd_channel(target_ch_id)
                    if ch_row and ch_row.get("max_queue_limit") is not None:
                        ch_limit = ch_row["max_queue_limit"]
                await interaction.followup.send(t(self.lang, "sugg_cannot_approve_queue", max_q=ch_limit), ephemeral=True)
            elif reason == "total_limit":
                await interaction.followup.send(t(self.lang, "sugg_cannot_approve_total", max_total=MAX_TOTAL_QUESTIONS), ephemeral=True)
            else:
                await interaction.followup.send(t(self.lang, "sugg_approve_failed"), ephemeral=True)

    async def on_edit(self, interaction: discord.Interaction):
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        target_ch_id = self.suggestion_row.get("target_channel_id")
        modal = EditSuggestionModal(
            self.qotd,
            self.guild_id,
            self.suggestion_id,
            self.suggestion_row["question"],
            channels=channels,
            target_channel_id=target_ch_id,
            return_page=self.return_page,
            from_panel=True,
            lang=self.lang,
        )
        await interaction.response.send_modal(modal)

    async def on_decline(self, interaction: discord.Interaction):
        modal = RejectSuggestionModal(self.qotd, self.guild_id, self.suggestion_id, return_page=self.return_page, from_panel=True, lang=self.lang)
        await interaction.response.send_modal(modal)

    async def on_back(self, interaction: discord.Interaction):
        total_pages = await self.qotd.suggestions_page_count(self.guild_id)
        target_page = min(self.return_page, max(0, total_pages - 1))
        embed = await self.qotd.build_suggestions_embed(self.guild_id, target_page, lang=self.lang)
        page_items = await self.qotd.get_suggestions_page(self.guild_id, target_page)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        view = QotdPanelView(
            self.qotd,
            section="suggestions",
            page=target_page,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channels=channels,
            lang=self.lang,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)


class BulkDeleteSelectView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, status: str, page: int, rows: List[Any], channel_id: Optional[int] = None, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.status = status
        self.page = page
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        options = [
            discord.SelectOption(
                label=f"#{row['id']} • {row['question'][:80]}",
                value=str(row["id"]),
            )
            for row in rows[:25]
        ]
        if not options:
            options = [discord.SelectOption(label=t(self.lang, "no_questions_page"), value="none")]

        placeholder = t(self.lang, "select_delete_placeholder")
        self.select_menu = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=len(options) if options[0].value != "none" else 1,
            options=options,
            row=0,
        )
        self.select_menu.callback = self.on_select_change
        self.add_item(self.select_menu)

        btn_confirm = discord.ui.Button(label=t(self.lang, "btn_bulk_delete"), style=discord.ButtonStyle.danger, emoji=Icon.TRASH, row=1)
        btn_confirm.callback = self.on_confirm_selected
        self.add_item(btn_confirm)

        btn_cancel = discord.ui.Button(label=t(self.lang, "btn_cancel"), style=discord.ButtonStyle.secondary, emoji=Icon.CANCEL, row=1)
        btn_cancel.callback = self.on_cancel
        self.add_item(btn_cancel)

    async def on_select_change(self, interaction: discord.Interaction):
        await interaction.response.defer()

    async def on_confirm_selected(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return

        selected_ids = [int(v) for v in self.select_menu.values if v.isdigit()]
        if not selected_ids:
            await interaction.response.send_message(t(self.lang, "no_items_selected"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        deleted_count = await self.qotd.delete_multiple_questions(self.guild_id, selected_ids, channel_id=self.channel_id)
        await interaction.followup.send(t(self.lang, "bulk_delete_confirm_msg", count=deleted_count), ephemeral=True)

        channels = await self.qotd.get_qotd_channels(self.guild_id)
        embed = await self.qotd.build_questions_embed(self.guild_id, self.status, self.page, channel_id=self.channel_id, lang=self.lang)
        total_pages = await self.qotd.question_page_count(self.guild_id, self.status, channel_id=self.channel_id)
        page_items = await self.qotd.get_questions_page(self.guild_id, self.status, self.page, channel_id=self.channel_id)
        view = QotdPanelView(
            self.qotd,
            section=self.status,
            page=self.page,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channel_id=self.channel_id,
            channels=channels,
            lang=self.lang,
        )
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        embed = await self.qotd.build_questions_embed(self.guild_id, self.status, self.page, channel_id=self.channel_id, lang=self.lang)
        total_pages = await self.qotd.question_page_count(self.guild_id, self.status, channel_id=self.channel_id)
        page_items = await self.qotd.get_questions_page(self.guild_id, self.status, self.page, channel_id=self.channel_id)
        view = QotdPanelView(
            self.qotd,
            section=self.status,
            page=self.page,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channel_id=self.channel_id,
            channels=channels,
            lang=self.lang,
        )
        await interaction.edit_original_response(embed=embed, view=view)


class SearchResultsView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, results: List[Any], query_str: str, return_section: str = "to_ask", channel_id: Optional[int] = None, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.return_section = return_section
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        by_prefix = t(self.lang, "author_prefix")
        sys_default = t(self.lang, "source_system")
        queue_label = t(self.lang, "queue_label")
        history_label = t(self.lang, "history_label")

        options = [
            discord.SelectOption(
                label=f"#{row['id']} • {row['question'][:75]}",
                value=str(row["id"]),
                description=f"{queue_label if row['status'] == 'to_ask' else history_label} | {by_prefix}: {(row['suggested_by_name'] or row['added_by_name'] or sys_default)[:30]}",
            )
            for row in results[:25]
        ]
        if not options:
            options = [discord.SelectOption(label=t(self.lang, "no_results"), value="none")]

        placeholder = t(self.lang, "select_search_detail_placeholder")
        select_menu = discord.ui.Select(placeholder=placeholder, options=options, row=0)
        select_menu.callback = self.on_select
        self.add_item(select_menu)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji=Icon.ARROW_LEFT, row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.data.get("values", [""])[0] == "none":
            await interaction.response.defer()
            return
        await interaction.response.defer()
        q_id = int(interaction.data["values"][0])
        q_row = await self.qotd.get_question(self.guild_id, q_id)
        if not q_row:
            await interaction.followup.send(t(self.lang, "not_found"), ephemeral=True)
            return

        embed = await self.qotd.build_question_detail_embed(self.guild_id, q_row, lang=self.lang)
        view = QuestionDetailView(self.qotd, self.guild_id, q_row, return_section=self.return_section, return_page=0, channel_id=self.channel_id, lang=self.lang)
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        embed = await self.qotd.build_questions_embed(self.guild_id, self.return_section, 0, channel_id=self.channel_id, lang=self.lang)
        total_pages = await self.qotd.question_page_count(self.guild_id, self.return_section, channel_id=self.channel_id)
        page_items = await self.qotd.get_questions_page(self.guild_id, self.return_section, 0, channel_id=self.channel_id)
        view = QotdPanelView(
            self.qotd,
            section=self.return_section,
            page=0,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channel_id=self.channel_id,
            channels=channels,
            lang=self.lang,
        )
        await interaction.edit_original_response(embed=embed, view=view)


class ClearQuestionsConfirmView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, status: str, page: int, channel_id: Optional[int] = None, lang: Optional[str] = None):
        super().__init__(timeout=120)
        self.qotd = qotd
        self.guild_id = guild_id
        self.status = status
        self.page = page
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        btn_confirm = discord.ui.Button(label=t(self.lang, "btn_yes_delete_all"), emoji=Icon.DELETE_FOREVER, style=discord.ButtonStyle.danger)
        btn_confirm.callback = self.confirm
        self.add_item(btn_confirm)

        btn_cancel = discord.ui.Button(label=t(self.lang, "btn_cancel"), emoji=Icon.CANCEL, style=discord.ButtonStyle.secondary)
        btn_cancel.callback = self.cancel
        self.add_item(btn_cancel)

    async def confirm(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await self.qotd.clear_questions(self.guild_id, self.status, channel_id=self.channel_id)
        await interaction.followup.send(t(self.lang, "clear_category_success", section=self.status, count=removed), ephemeral=True)

        if self.status == "suggestions":
            embed = await self.qotd.build_suggestions_embed(self.guild_id, 0, lang=self.lang)
            view = QotdPanelView(self.qotd, section="suggestions", page=0, guild_id=self.guild_id, total_pages=1, page_items=[], lang=self.lang)
        else:
            channels = await self.qotd.get_qotd_channels(self.guild_id)
            embed = await self.qotd.build_questions_embed(self.guild_id, self.status, 0, channel_id=self.channel_id, lang=self.lang)
            view = QotdPanelView(
                self.qotd,
                section=self.status,
                page=0,
                guild_id=self.guild_id,
                total_pages=1,
                page_items=[],
                channel_id=self.channel_id,
                channels=channels,
                lang=self.lang,
            )
        await interaction.edit_original_response(embed=embed, view=view)

    async def cancel(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return

        await interaction.response.defer()
        if self.status == "suggestions":
            embed = await self.qotd.build_suggestions_embed(self.guild_id, self.page, lang=self.lang)
            total_pages = await self.qotd.suggestions_page_count(self.guild_id)
            page_items = await self.qotd.get_suggestions_page(self.guild_id, self.page)
            view = QotdPanelView(self.qotd, section="suggestions", page=self.page, guild_id=self.guild_id, total_pages=total_pages, page_items=page_items, lang=self.lang)
        else:
            channels = await self.qotd.get_qotd_channels(self.guild_id)
            embed = await self.qotd.build_questions_embed(self.guild_id, self.status, self.page, channel_id=self.channel_id, lang=self.lang)
            total_pages = await self.qotd.question_page_count(self.guild_id, self.status, channel_id=self.channel_id)
            page_items = await self.qotd.get_questions_page(self.guild_id, self.status, self.page, channel_id=self.channel_id)
            view = QotdPanelView(
                self.qotd,
                section=self.status,
                page=self.page,
                guild_id=self.guild_id,
                total_pages=total_pages,
                page_items=page_items,
                channel_id=self.channel_id,
                channels=channels,
                lang=self.lang,
            )
        await interaction.edit_original_response(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Qotd Cog
# ---------------------------------------------------------------------------

