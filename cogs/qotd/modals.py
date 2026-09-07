import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING
import discord
from translations import t
from .constants import (
    logger,
    COLOR_DETAIL,
    QOTD_TIMEZONE,
    MAX_QUESTION_LENGTH,
    MAX_QUEUE_QUESTIONS,
    MAX_TOTAL_QUESTIONS,
    MAX_BATCH_ADD_QUESTIONS,
    parse_question_input,
    normalize_question,
    is_admin,
    Icon,
)

if TYPE_CHECKING:
    from .cog import Qotd

class BaseModal(discord.ui.Modal):
    """Base modal that logs errors and sends an ephemeral error message to the user
    instead of leaving the modal spinner hanging indefinitely."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Error in modal %s: %s", self.__class__.__name__, error)
        msg = f"{Icon.CANCEL} An unexpected error occurred while processing your input."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.DiscordException as d_err:
            logger.debug("Failed to send modal on_error response: %s", d_err)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class SuggestionModal(BaseModal):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        channels: Optional[List[Dict[str, Any]]] = None,
        target_channel_id: Optional[int] = None,
        prefilled_question: Optional[str] = None,
        prefilled_note: Optional[str] = None,
        lang: Optional[str] = None,
    ):
        self.qotd = qotd
        self.guild_id = guild_id
        self.channels = channels or []
        self.target_channel_id = target_channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_suggest_title")[:45])

        self.question = discord.ui.TextInput(
            label=t(self.lang, "modal_suggest_q_label")[:45],
            placeholder=t(self.lang, "modal_suggest_q_placeholder")[:100],
            default=prefilled_question or "",
            max_length=300,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.question)

        self.message = discord.ui.TextInput(
            label=t(self.lang, "modal_suggest_msg_label")[:45],
            placeholder=t(self.lang, "modal_suggest_msg_placeholder")[:100],
            default=prefilled_note or "",
            max_length=500,
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.message)

        self.channel_select: Optional[discord.ui.Select] = None
        self.channel_label: Optional[discord.ui.Label] = None
        if self.channels:
            options = []
            guild = qotd.bot.get_guild(guild_id)
            default_chosen = False
            for idx, ch in enumerate(self.channels[:25]):
                ch_id = ch["channel_id"]
                discord_ch = qotd.bot.get_channel(ch_id) or (guild.get_channel(ch_id) if guild else None)
                raw_name = discord_ch.name if discord_ch else str(ch_id)
                ch_name = f"#{raw_name}" if not raw_name.startswith("#") else raw_name
                is_default = False
                if target_channel_id is not None and ch_id == target_channel_id:
                    is_default = True
                    default_chosen = True
                elif target_channel_id is None and idx == 0:
                    is_default = True
                    default_chosen = True
                options.append(
                    discord.SelectOption(
                        label=ch_name[:100],
                        value=str(ch_id),
                        default=is_default,
                    )
                )

            if not default_chosen and options:
                options[0].default = True

            self.channel_select = discord.ui.Select(
                placeholder=t(self.lang, "select_channel_suggest_placeholder")[:100],
                options=options,
                min_values=1,
                max_values=1,
            )
            # Discord V2 Modals use Component Type 18 (Label) to wrap select menus
            self.channel_label = discord.ui.Label(
                text=t(self.lang, "channel_label")[:45],
                component=self.channel_select,
            )
            self.add_item(self.channel_label)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)

        selected_ch_id = None
        if self.channel_select and self.channel_select.values:
            try:
                selected_ch_id = int(self.channel_select.values[0])
            except (ValueError, TypeError):
                selected_ch_id = None

        if not selected_ch_id:
            selected_ch_id = self.target_channel_id
        if not selected_ch_id and self.channels:
            selected_ch_id = self.channels[0]["channel_id"]

        if not selected_ch_id or not await self.qotd.get_qotd_channel(selected_ch_id):
            await interaction.followup.send(t(user_lang, "channel_not_qotd_generic"), ephemeral=True)
            return

        ok, msg = await self.qotd.handle_user_suggestion(
            guild_id=self.guild_id,
            user=interaction.user,
            question_text=str(self.question),
            note=str(self.message).strip() or None,
            user_locale=getattr(interaction, "locale", None),
            lang=user_lang,
            target_channel_id=selected_ch_id,
        )
        await interaction.followup.send(msg, ephemeral=True)



class AddQuestionModal(BaseModal):
    def __init__(self, qotd: "Qotd", guild_id: int, current_page: int = 0, channel_id: Optional[int] = None, lang: Optional[str] = None):
        self.qotd = qotd
        self.guild_id = guild_id
        self.current_page = current_page
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_add_title"))

        self.questions_input = discord.ui.TextInput(
            label=t(self.lang, "modal_add_label"),
            placeholder=t(self.lang, "modal_add_placeholder"),
            max_length=4000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.questions_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        raw_text = str(self.questions_input)
        parsed = parse_question_input(raw_text)
        if not parsed:
            await interaction.followup.send(t(user_lang, "no_valid_questions"), ephemeral=True)
            return

        if len(parsed) > MAX_BATCH_ADD_QUESTIONS:
            await interaction.followup.send(
                t(user_lang, "limit_batch_size", max_batch=MAX_BATCH_ADD_QUESTIONS, len=len(parsed)),
                ephemeral=True,
            )
            return

        too_long = [q for q in parsed if len(q) > MAX_QUESTION_LENGTH]
        if too_long:
            sample = too_long[0] if len(too_long[0]) <= 50 else f"{too_long[0][:50]}..."
            await interaction.followup.send(
                t(user_lang, "limit_batch_too_long", max_len=MAX_QUESTION_LENGTH, count=len(too_long), sample=sample, len=len(too_long[0])),
                ephemeral=True,
            )
            return

        cur_queue = await self.qotd.get_queue_count(self.guild_id, self.channel_id)
        channel_max_limit = MAX_QUEUE_QUESTIONS
        if self.channel_id:
            ch_row = await self.qotd.get_qotd_channel(self.channel_id)
            if ch_row and ch_row.get("max_queue_limit") is not None:
                channel_max_limit = ch_row["max_queue_limit"]

        if cur_queue >= channel_max_limit:
            await interaction.followup.send(
                t(user_lang, "limit_queue_full", max_q=channel_max_limit),
                ephemeral=True,
            )
            return

        cur_total = await self.qotd.get_total_question_count(self.guild_id)
        if cur_total >= MAX_TOTAL_QUESTIONS:
            await interaction.followup.send(
                t(user_lang, "limit_total_full", max_total=MAX_TOTAL_QUESTIONS),
                ephemeral=True,
            )
            return

        unique, duplicates = await self.qotd.check_duplicates(self.guild_id, parsed, channel_id=self.channel_id)
        if not unique:
            lines = [t(user_lang, "no_new_questions_added")]
            if duplicates:
                lines.append(t(user_lang, "all_duplicates", count=len(duplicates)))
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

        remaining_slots = max(0, min(channel_max_limit - cur_queue, MAX_TOTAL_QUESTIONS - cur_total))
        if remaining_slots <= 0:
            await interaction.followup.send(
                t(user_lang, "limit_queue_full", max_q=channel_max_limit),
                ephemeral=True,
            )
            return
        to_add = unique[:remaining_slots]
        skipped_capacity = len(unique) - len(to_add)

        added = []
        for q in to_add:
            qid = await self.qotd.add_question(
                self.guild_id,
                q,
                source="manual",
                added_by=interaction.user,
                channel_id=self.channel_id,
            )
            if qid:
                added.append(qid)

        lines = []
        if added:
            lines.append(t(user_lang, "added_new_questions", count=len(added)))
        if duplicates:
            dup_cnt = len(duplicates)
            lines.append(t(user_lang, "skipped_duplicates", count=dup_cnt))
            for d in duplicates[:3]:
                lines.append(f"  • {d[:60]}")
            if dup_cnt > 3:
                lines.append(t(user_lang, "and_more_duplicates", count=dup_cnt - 3))
        if skipped_capacity > 0:
            lines.append(t(user_lang, "skipped_capacity", count=skipped_capacity, max_q=channel_max_limit, max_total=MAX_TOTAL_QUESTIONS))

        msg_text = "\n".join(lines) if lines else t(user_lang, "no_new_questions_added")
        await interaction.followup.send(msg_text, ephemeral=True)

        channels = await self.qotd.get_qotd_channels(self.guild_id)
        embed = await self.qotd.build_questions_embed(self.guild_id, "to_ask", self.current_page, channel_id=self.channel_id, lang=user_lang)
        total_pages = await self.qotd.question_page_count(self.guild_id, "to_ask", channel_id=self.channel_id)
        page_items = await self.qotd.get_questions_page(self.guild_id, "to_ask", self.current_page, channel_id=self.channel_id)
        from .views import QotdPanelView
        view = QotdPanelView(
            self.qotd,
            section="to_ask",
            page=self.current_page,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channel_id=self.channel_id,
            channels=channels,
            lang=user_lang,
        )
        await interaction.edit_original_response(embed=embed, view=view)


class EditQuestionModal(BaseModal):
    def __init__(self, qotd: "Qotd", guild_id: int, question_id: int, current_text: str, return_section: str = "to_ask", return_page: int = 0, channel_id: Optional[int] = None, lang: Optional[str] = None):
        self.qotd = qotd
        self.guild_id = guild_id
        self.question_id = question_id
        self.return_section = return_section
        self.return_page = return_page
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_edit_q_title"))

        self.question_input = discord.ui.TextInput(
            label=t(self.lang, "modal_edit_q_label"),
            default=current_text,
            max_length=300,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.question_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        new_text = str(self.question_input).strip()
        if not new_text:
            await interaction.response.send_message(t(user_lang, "limit_empty_question"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        cursor = await self.qotd.db.execute(
            "UPDATE questions SET question = ? WHERE guild_id = ? AND id = ?",
            (new_text, self.guild_id, self.question_id),
        )
        await self.qotd.db.commit()

        if cursor.rowcount > 0:
            await interaction.followup.send(t(user_lang, "edit_success"), ephemeral=True)
        else:
            await interaction.followup.send(t(user_lang, "edit_not_found"), ephemeral=True)

        updated_row = await self.qotd.get_question(self.guild_id, self.question_id)
        if updated_row:
            embed = await self.qotd.build_question_detail_embed(self.guild_id, updated_row, lang=user_lang)
            from .views import QuestionDetailView
            view = QuestionDetailView(self.qotd, self.guild_id, updated_row, return_section=self.return_section, return_page=self.return_page, channel_id=self.channel_id, lang=user_lang)
            await interaction.edit_original_response(embed=embed, view=view)


class EditSuggestionModal(BaseModal):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        suggestion_id: str,
        current_text: str,
        channels: Optional[List[Dict[str, Any]]] = None,
        target_channel_id: Optional[int] = None,
        return_page: int = 0,
        from_panel: bool = False,
        lang: Optional[str] = None,
    ):
        self.qotd = qotd
        self.guild_id = guild_id
        self.suggestion_id = suggestion_id
        self.channels = channels or []
        self.target_channel_id = target_channel_id
        self.return_page = return_page
        self.from_panel = from_panel
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_edit_sugg_title")[:45])

        self.question_input = discord.ui.TextInput(
            label=t(self.lang, "modal_edit_sugg_label")[:45],
            default=current_text,
            max_length=300,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.question_input)

        self.channel_select: Optional[discord.ui.Select] = None
        self.channel_label: Optional[discord.ui.Label] = None
        if self.channels:
            options = []
            guild = qotd.bot.get_guild(guild_id) if hasattr(qotd, "bot") else None
            default_chosen = False
            for idx, ch in enumerate(self.channels[:25]):
                ch_id = ch["channel_id"]
                discord_ch = (qotd.bot.get_channel(ch_id) or (guild.get_channel(ch_id) if guild else None)) if hasattr(qotd, "bot") else None
                raw_name = discord_ch.name if discord_ch else str(ch_id)
                ch_name = f"#{raw_name}" if not raw_name.startswith("#") else raw_name
                is_default = False
                if target_channel_id is not None and ch_id == target_channel_id:
                    is_default = True
                    default_chosen = True
                elif target_channel_id is None and idx == 0:
                    is_default = True
                    default_chosen = True
                options.append(
                    discord.SelectOption(
                        label=ch_name[:100],
                        value=str(ch_id),
                        default=is_default,
                    )
                )

            if not default_chosen and options:
                options[0].default = True

            self.channel_select = discord.ui.Select(
                placeholder=t(self.lang, "select_channel_suggest_placeholder")[:100],
                options=options,
                min_values=1,
                max_values=1,
            )
            self.channel_label = discord.ui.Label(
                text=t(self.lang, "channel_label")[:45],
                component=self.channel_select,
            )
            self.add_item(self.channel_label)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        new_text = str(self.question_input).strip()
        if not new_text:
            await interaction.response.send_message(t(user_lang, "limit_empty_question"), ephemeral=True)
            return

        selected_ch_id = None
        if self.channel_select and self.channel_select.values:
            try:
                selected_ch_id = int(self.channel_select.values[0])
            except (ValueError, TypeError):
                selected_ch_id = None
        if not selected_ch_id:
            selected_ch_id = self.target_channel_id
        if not selected_ch_id and self.channels:
            selected_ch_id = self.channels[0]["channel_id"]

        await interaction.response.defer()

        if self.from_panel:
            ok, reason = await self.qotd.accept_suggestion(
                self.guild_id,
                self.suggestion_id,
                question=new_text,
                review_message=None,
                added_by=interaction.user,
                interaction=None,
                target_channel_id=selected_ch_id,
            )
            if ok:
                total_pages = await self.qotd.suggestions_page_count(self.guild_id)
                target_page = min(self.return_page, max(0, total_pages - 1))
                embed = await self.qotd.build_suggestions_embed(self.guild_id, target_page, lang=user_lang)
                page_items = await self.qotd.get_suggestions_page(self.guild_id, target_page)
                channels = await self.qotd.get_qotd_channels(self.guild_id)
                from .views import QotdPanelView
                view = QotdPanelView(
                    self.qotd,
                    section="suggestions",
                    page=target_page,
                    guild_id=self.guild_id,
                    total_pages=total_pages,
                    page_items=page_items,
                    channels=channels,
                    lang=user_lang,
                )
                try:
                    await interaction.edit_original_response(embed=embed, view=view)
                except discord.DiscordException as err:
                    logger.debug("Failed to edit suggestion detail response: %s", err)
            else:
                if reason == "queue_full":
                    ch_limit = MAX_QUEUE_QUESTIONS
                    if selected_ch_id:
                        ch_row = await self.qotd.get_qotd_channel(selected_ch_id)
                        if ch_row and ch_row.get("max_queue_limit") is not None:
                            ch_limit = ch_row["max_queue_limit"]
                    await interaction.followup.send(t(user_lang, "sugg_cannot_approve_queue", max_q=ch_limit), ephemeral=True)
                elif reason == "total_limit":
                    await interaction.followup.send(t(user_lang, "sugg_cannot_approve_total", max_total=MAX_TOTAL_QUESTIONS), ephemeral=True)
                else:
                    await interaction.followup.send(t(user_lang, "sugg_approve_failed"), ephemeral=True)
        else:
            ok, reason = await self.qotd.accept_suggestion(
                self.guild_id,
                self.suggestion_id,
                question=new_text,
                review_message=interaction.message,
                added_by=interaction.user,
                interaction=interaction,
                target_channel_id=selected_ch_id,
            )
            if not ok:
                if reason == "queue_full":
                    ch_limit = MAX_QUEUE_QUESTIONS
                    if selected_ch_id:
                        ch_row = await self.qotd.get_qotd_channel(selected_ch_id)
                        if ch_row and ch_row.get("max_queue_limit") is not None:
                            ch_limit = ch_row["max_queue_limit"]
                    await interaction.followup.send(t(user_lang, "sugg_cannot_approve_queue", max_q=ch_limit), ephemeral=True)
                elif reason == "total_limit":
                    await interaction.followup.send(t(user_lang, "sugg_cannot_approve_total", max_total=MAX_TOTAL_QUESTIONS), ephemeral=True)
                else:
                    await interaction.followup.send(t(user_lang, "sugg_approve_failed"), ephemeral=True)


class RejectSuggestionModal(BaseModal):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        suggestion_id: str,
        return_page: int = 0,
        from_panel: bool = False,
        lang: Optional[str] = None,
    ):
        self.qotd = qotd
        self.guild_id = guild_id
        self.suggestion_id = suggestion_id
        self.return_page = return_page
        self.from_panel = from_panel
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_reject_title"))

        self.reason_input = discord.ui.TextInput(
            label=t(self.lang, "modal_reject_label"),
            placeholder=t(self.lang, "modal_reject_placeholder"),
            max_length=500,
            required=False,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        reason = str(self.reason_input).strip()
        await interaction.response.defer()

        if self.from_panel:
            ok = await self.qotd.reject_suggestion(
                self.guild_id,
                self.suggestion_id,
                reason=reason,
                review_message=None,
                interaction=None,
            )
            if ok:
                emb = discord.Embed(
                    title=t(user_lang, "sugg_detail_title"),
                    description=t(user_lang, "sugg_rejected_msg"),
                    colour=discord.Colour.red(),
                )
                emb.set_footer(text="❌ " + t(user_lang, "btn_decline"))
                if reason:
                    emb.add_field(name=t(user_lang, "sugg_dm_rejected_reason"), value=reason, inline=False)

                from .views import SuggestionDeclinedView
                view = SuggestionDeclinedView(
                    self.qotd,
                    self.guild_id,
                    return_page=self.return_page,
                    lang=user_lang,
                )
                await interaction.edit_original_response(embed=emb, view=view)
            else:
                await interaction.followup.send(t(user_lang, "sugg_reject_failed"), ephemeral=True)
        else:
            ok = await self.qotd.reject_suggestion(
                self.guild_id,
                self.suggestion_id,
                reason=reason,
                review_message=interaction.message,
                interaction=interaction,
            )
            if not ok:
                await interaction.followup.send(t(user_lang, "sugg_reject_failed"), ephemeral=True)


class SearchQuestionsModal(BaseModal):
    def __init__(self, qotd: "Qotd", guild_id: int, status_filter: str = "to_ask", channel_id: Optional[int] = None, lang: Optional[str] = None):
        self.qotd = qotd
        self.guild_id = guild_id
        self.status_filter = status_filter
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_search_title"))

        self.query = discord.ui.TextInput(
            label=t(self.lang, "modal_search_label"),
            placeholder=t(self.lang, "modal_search_placeholder"),
            max_length=100,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        search_str = str(self.query).strip()
        results = await self.qotd.search_questions(self.guild_id, search_str, status=self.status_filter, channel_id=self.channel_id)
        if not results:
            await interaction.followup.send(t(user_lang, "search_no_results"), ephemeral=True)
            return

        desc = t(user_lang, "search_results_desc", count=len(results))
        embed = discord.Embed(
            title=t(user_lang, "search_results_title", query=search_str),
            description=desc,
            colour=COLOR_DETAIL,
        )
        for idx, row in enumerate(results[:10], 1):
            source_icon = Icon.BULB if row["source"] == "suggestion" else (Icon.ACCOUNT if row["source"] == "manual" else Icon.SMART_TOY)
            status_text = t(user_lang, "status_queue") if row["status"] == "to_ask" else t(user_lang, "status_asked")
            embed.add_field(
                name=f"{idx}. [#{row['id']}] {status_text} • {source_icon}",
                value=row["question"][:150],
                inline=False,
            )

        from .views import SearchResultsView
        view = SearchResultsView(self.qotd, self.guild_id, results, search_str, return_section=self.status_filter, channel_id=self.channel_id, lang=user_lang)
        await interaction.edit_original_response(embed=embed, view=view)


class QotdHourModal(BaseModal):
    def __init__(self, qotd: "Qotd", guild_id: int, channel_id: int, current_time: str, lang: Optional[str] = None):
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_time_title"))

        self.scheduled_time = discord.ui.TextInput(
            label=t(self.lang, "modal_time_label"),
            placeholder=t(self.lang, "modal_time_placeholder"),
            default=current_time,
            max_length=5,
        )
        self.add_item(self.scheduled_time)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        scheduled_time = str(self.scheduled_time).strip()
        if not re.fullmatch(r"^(?:[01]\d|2[0-3]):[0-5]\d$", scheduled_time):
            await interaction.response.send_message(t(user_lang, "time_invalid"), ephemeral=True)
            return

        now_local = datetime.now(QOTD_TIMEZONE)
        today = now_local.date().isoformat()
        current_minute = now_local.time().replace(second=0, microsecond=0)
        try:
            s_time = datetime.strptime(scheduled_time, "%H:%M").time()
            if s_time >= current_minute:
                # Scheduled for current minute or later today:
                # Clear last_posted_date so the bot will post when this time arrives today!
                await self.qotd.update_qotd_channel(self.channel_id, scheduled_time=scheduled_time, last_posted_date=None)
            else:
                # Scheduled time has already passed today (e.g. setting 09:00 at 18:50 for tomorrow morning):
                # Mark as today so it waits for tomorrow and does not immediately blast right now.
                await self.qotd.update_qotd_channel(self.channel_id, scheduled_time=scheduled_time, last_posted_date=today)
        except (ValueError, TypeError):
            await self.qotd.update_qotd_channel(self.channel_id, scheduled_time=scheduled_time)

        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=user_lang)
        from .views import ChannelManageView
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=user_lang)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(t(user_lang, "time_updated", time=scheduled_time), ephemeral=True)


class QotdThresholdModal(BaseModal):
    def __init__(self, qotd: "Qotd", guild_id: int, channel_id: int, current_thresh: int = 3, lang: Optional[str] = None):
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_threshold_title"))

        self.threshold_input = discord.ui.TextInput(
            label=t(self.lang, "modal_threshold_label"),
            default=str(current_thresh),
            max_length=3,
        )
        self.add_item(self.threshold_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        raw = str(self.threshold_input).strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 50):
            await interaction.response.send_message("⚠️ Please enter a number between 1 and 50.", ephemeral=True)
            return

        val = int(raw)
        await self.qotd.update_qotd_channel(self.channel_id, low_queue_threshold=val)
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=user_lang)
        from .views import ChannelManageView
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=user_lang)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(t(user_lang, "threshold_updated", threshold=val), ephemeral=True)


class QotdMaxQueueModal(BaseModal):
    def __init__(self, qotd: "Qotd", guild_id: int, channel_id: int, current_max: int = 500, lang: Optional[str] = None):
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_max_queue_title")[:45])

        self.max_input = discord.ui.TextInput(
            label=t(self.lang, "modal_max_queue_label")[:45],
            default=str(current_max),
            max_length=4,
        )
        self.add_item(self.max_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        raw = str(self.max_input).strip()
        if not raw.isdigit() or not (5 <= int(raw) <= 1000):
            await interaction.response.send_message("⚠️ Please enter a number between 5 and 1000.", ephemeral=True)
            return

        val = int(raw)
        await self.qotd.update_qotd_channel(self.channel_id, max_queue_limit=val)
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=user_lang)
        from .views import ChannelManageView
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=user_lang)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(t(user_lang, "max_queue_updated", max_queue=val), ephemeral=True)


# ---------------------------------------------------------------------------
# Dynamic Review Button (Persistent admin channel notification)
# ---------------------------------------------------------------------------

