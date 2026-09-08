import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union, TYPE_CHECKING
import discord
from translations import t, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE
from .constants import (
    logger,
    COLOR_DETAIL,
    QOTD_TIMEZONE,
    MAX_QUESTION_LENGTH,
    MAX_QUEUE_QUESTIONS,
    MAX_TOTAL_QUESTIONS,
    MAX_BATCH_ADD_QUESTIONS,
    MAX_FILE_UPLOAD_QUESTIONS,
    parse_question_input,
    normalize_question,
    is_admin,
    Icon,
    DEFAULT_SCHEDULED_TIME,
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
            required=False,
        )
        self.add_item(self.questions_input)

        self.file_upload = discord.ui.FileUpload(
            required=False,
            max_values=1,
        )
        self.file_label = discord.ui.Label(
            text=t(self.lang, "modal_upload_file_label")[:45],
            description=t(self.lang, "modal_upload_file_desc")[:100],
            component=self.file_upload,
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        raw_text = str(self.questions_input).strip() if self.questions_input.value else ""
        text_parsed = parse_question_input(raw_text) if raw_text else []
        if len(text_parsed) > MAX_BATCH_ADD_QUESTIONS:
            await interaction.followup.send(
                t(user_lang, "limit_batch_size", max_batch=MAX_BATCH_ADD_QUESTIONS, len=len(text_parsed)),
                ephemeral=True,
            )
            return

        file_parsed = []
        file_attachments = getattr(self.file_upload, "values", None) or []
        if file_attachments:
            attachment = file_attachments[0]
            if not attachment.filename.lower().endswith(".txt"):
                await interaction.followup.send(t(user_lang, "invalid_file_extension"), ephemeral=True)
                return
            if getattr(attachment, "size", 0) > 2 * 1024 * 1024:
                await interaction.followup.send(t(user_lang, "file_too_large"), ephemeral=True)
                return
            try:
                content_bytes = await attachment.read()
                try:
                    file_text = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    file_text = content_bytes.decode("latin-1", errors="replace")
                file_parsed = parse_question_input(file_text)
            except Exception as err:
                logger.error("Failed to read uploaded .txt file: %s", err)
                await interaction.followup.send(t(user_lang, "file_read_error"), ephemeral=True)
                return

            if len(file_parsed) > MAX_FILE_UPLOAD_QUESTIONS:
                await interaction.followup.send(
                    t(user_lang, "file_too_many_questions", max_file=MAX_FILE_UPLOAD_QUESTIONS, count=len(file_parsed)),
                    ephemeral=True,
                )
                return

        parsed = text_parsed + file_parsed
        if not parsed:
            await interaction.followup.send(t(user_lang, "no_valid_questions"), ephemeral=True)
            return

        too_long = [q for q in parsed if len(q) > MAX_QUESTION_LENGTH]
        if too_long:
            sample = too_long[0] if len(too_long[0]) <= 50 else f"{too_long[0][:50]}..."
            await interaction.followup.send(
                t(user_lang, "limit_batch_too_long", max_len=MAX_QUESTION_LENGTH, count=len(too_long), sample=sample, len=len(too_long[0])),
                ephemeral=True,
            )
            return

        channels = await self.qotd.get_qotd_channels(self.guild_id)
        if not channels:
            await interaction.followup.send(t(user_lang, "no_channels_configured"), ephemeral=True)
            return

        target_ch_id = self.channel_id
        if target_ch_id not in [c["channel_id"] for c in channels]:
            target_ch_id = channels[0]["channel_id"]

        unique, duplicates = await self.qotd.check_duplicates(self.guild_id, parsed, channel_id=target_ch_id)
        if not unique:
            lines = [t(user_lang, "no_new_questions_added")]
            if duplicates:
                lines.append(t(user_lang, "all_duplicates", count=len(duplicates)))
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

        ch_row = await self.qotd.get_qotd_channel(target_ch_id)
        channel_max_limit = (ch_row.get("max_queue_limit") if ch_row else None) or MAX_QUEUE_QUESTIONS

        added, skipped_capacity, limit_reason = await self.qotd.add_multiple_questions(
            self.guild_id,
            unique,
            source="manual",
            added_by=interaction.user,
            channel_id=target_ch_id,
        )

        if not added and skipped_capacity > 0 and not duplicates:
            if limit_reason == "total_limit":
                await interaction.followup.send(
                    t(user_lang, "limit_total_full", max_total=MAX_TOTAL_QUESTIONS),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    t(user_lang, "limit_queue_full", max_q=channel_max_limit),
                    ephemeral=True,
                )
            return

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

        embed = await self.qotd.build_questions_embed(self.guild_id, "to_ask", self.current_page, channel_id=target_ch_id, lang=user_lang)
        total_pages = await self.qotd.question_page_count(self.guild_id, "to_ask", channel_id=target_ch_id)
        page_items = await self.qotd.get_questions_page(self.guild_id, "to_ask", self.current_page, channel_id=target_ch_id)
        from .views import QotdPanelView
        view = QotdPanelView(
            self.qotd,
            section="to_ask",
            page=self.current_page,
            guild_id=self.guild_id,
            total_pages=total_pages,
            page_items=page_items,
            channel_id=target_ch_id,
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


class AddQotdChannelModal(BaseModal):
    def __init__(self, qotd: "Qotd", guild_id: int, lang: Optional[str] = None):
        self.qotd = qotd
        self.guild_id = guild_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "btn_add_qotd_channel")[:45])

        self.channel_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder=t(self.lang, "select_channel_add_placeholder")[:100],
            min_values=1,
            max_values=1,
            required=True,
        )
        self.channel_label = discord.ui.Label(
            text=t(self.lang, "channel_label")[:45],
            component=self.channel_select,
        )
        self.add_item(self.channel_label)

        self.schedule_input = discord.ui.TextInput(
            label=t(self.lang, "modal_time_label")[:45],
            placeholder=t(self.lang, "modal_time_placeholder")[:100],
            min_length=5,
            max_length=5,
            required=True,
        )
        self.add_item(self.schedule_input)

        self.role_select = discord.ui.RoleSelect(
            placeholder=t(self.lang, "select_ping_role")[:100],
            min_values=0,
            max_values=1,
            required=False,
        )
        self.role_label = discord.ui.Label(
            text=t(self.lang, "btn_manage_channel_role")[:45],
            component=self.role_select,
        )
        self.add_item(self.role_label)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        if not self.channel_select.values:
            await interaction.response.send_message(t(user_lang, "not_found"), ephemeral=True)
            return

        try:
            val = self.channel_select.values[0]
            channel_id = int(val.id) if hasattr(val, "id") else int(val)
        except (IndexError, ValueError, TypeError):
            await interaction.response.send_message(t(user_lang, "not_found"), ephemeral=True)
            return

        existing = await self.qotd.get_qotd_channel(channel_id)
        if existing:
            await interaction.response.send_message(t(user_lang, "channel_already_exists"), ephemeral=True)
            return

        schedule_time = str(self.schedule_input).strip()
        if not re.fullmatch(r"^(?:[01]\d|2[0-3]):[0-5]\d$", schedule_time):
            await interaction.response.send_message(t(user_lang, "time_invalid"), ephemeral=True)
            return

        role_id: Optional[int] = None
        if self.role_select.values:
            try:
                r_val = self.role_select.values[0]
                role_id = int(r_val.id) if hasattr(r_val, "id") else int(r_val)
            except (ValueError, TypeError, IndexError):
                role_id = None

        now_local = datetime.now(QOTD_TIMEZONE)
        today = now_local.date().isoformat()
        current_minute = now_local.time().replace(second=0, microsecond=0)
        last_posted = None
        try:
            s_time = datetime.strptime(schedule_time, "%H:%M").time()
            if s_time < current_minute:
                last_posted = today
        except (ValueError, TypeError):
            pass

        await self.qotd.add_qotd_channel(
            self.guild_id,
            channel_id,
            role_id=role_id,
            scheduled_time=schedule_time,
            last_posted_date=last_posted,
        )
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=user_lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        from .views import QotdPanelView
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=user_lang)

        msg_content = t(user_lang, "channel_added", channel_id=channel_id)
        if interaction.message and not interaction.response.is_done():
            await interaction.response.edit_message(content=msg_content, embed=embed, view=view)
        elif not interaction.response.is_done():
            await interaction.response.send_message(msg_content, ephemeral=True)
        else:
            await interaction.followup.send(msg_content, ephemeral=True)


class GuildSettingsModal(BaseModal):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        current_admin_channel_id: Optional[int] = None,
        current_language: Optional[str] = None,
        current_suggest_role_id: Optional[int] = None,
        lang: Optional[str] = None,
    ):
        self.qotd = qotd
        self.guild_id = guild_id
        self.current_admin_channel_id = current_admin_channel_id
        self.current_language = current_language or qotd.get_server_language(guild_id)
        self.current_suggest_role_id = current_suggest_role_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_settings_title")[:45])

        # 1. Admin Review Channel (ChannelSelect)
        admin_default = [discord.Object(id=current_admin_channel_id)] if current_admin_channel_id else discord.utils.MISSING
        self.admin_select = discord.ui.ChannelSelect(
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
            ],
            placeholder=t(self.lang, "select_admin_channel")[:100],
            min_values=0,
            max_values=1,
            required=False,
            default_values=admin_default,
        )
        self.admin_label = discord.ui.Label(
            text=t(self.lang, "settings_admin_channel")[:45],
            component=self.admin_select,
        )
        self.add_item(self.admin_label)

        # 2. Bot Language (StringSelect)
        flags = {
            "en": "🇬🇧",
            "cs": "🇨🇿",
            "es": "🇪🇸",
            "pt": "🇵🇹",
            "sk": "🇸🇰",
            "de": "🇩🇪",
            "fr": "🇫🇷",
        }
        cur_lang = self.current_language if self.current_language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
        lang_options = [
            discord.SelectOption(
                label=name,
                value=code,
                emoji=flags.get(code),
                default=(code == cur_lang),
            )
            for code, name in SUPPORTED_LANGUAGES.items()
        ]
        self.lang_select = discord.ui.Select(
            placeholder=t(self.lang, "select_lang_placeholder")[:100],
            options=lang_options,
            min_values=1,
            max_values=1,
            required=True,
        )
        self.lang_label = discord.ui.Label(
            text=t(self.lang, "btn_change_language")[:45],
            component=self.lang_select,
        )
        self.add_item(self.lang_label)

        # 3. Suggestion Role (RoleSelect)
        role_default = [discord.Object(id=current_suggest_role_id)] if current_suggest_role_id else discord.utils.MISSING
        self.role_select = discord.ui.RoleSelect(
            placeholder=t(self.lang, "select_suggest_role_placeholder")[:100],
            min_values=0,
            max_values=1,
            required=False,
            default_values=role_default,
        )
        self.role_label = discord.ui.Label(
            text=t(self.lang, "settings_suggest_role")[:45],
            component=self.role_select,
        )
        self.add_item(self.role_label)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        # 1. Admin Channel
        admin_channel_id: Optional[int] = None
        if self.admin_select.values:
            try:
                val = self.admin_select.values[0]
                admin_channel_id = int(val.id) if hasattr(val, "id") else int(val)
            except (IndexError, ValueError, TypeError):
                admin_channel_id = None

        # 2. Language
        selected_lang = self.current_language
        if self.lang_select.values:
            val = self.lang_select.values[0]
            val_str = val.value if hasattr(val, "value") else str(val)
            if val_str in SUPPORTED_LANGUAGES:
                selected_lang = val_str

        # 3. Suggestion Role
        suggest_role_id: Optional[int] = None
        if self.role_select.values:
            try:
                val = self.role_select.values[0]
                suggest_role_id = int(val.id) if hasattr(val, "id") else int(val)
            except (IndexError, ValueError, TypeError):
                suggest_role_id = None

        if selected_lang != self.current_language:
            await self.qotd.set_guild_language(self.guild_id, selected_lang)

        await self.qotd.update_guild_settings(
            self.guild_id,
            admin_channel_id=admin_channel_id,
            suggest_role_id=suggest_role_id,
        )

        effective_lang = selected_lang or user_lang
        embed = await self.qotd.build_settings_embed(self.guild_id, lang=effective_lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        from .views import QotdPanelView
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=effective_lang)

        msg_content = t(effective_lang, "setup_saved")
        if interaction.message and not interaction.response.is_done():
            await interaction.response.edit_message(content=msg_content, embed=embed, view=view)
        elif not interaction.response.is_done():
            await interaction.response.send_message(msg_content, ephemeral=True)
        else:
            await interaction.followup.send(msg_content, ephemeral=True)


class ChannelSettingsModal(BaseModal):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        channel_id: int,
        current_time: str = DEFAULT_SCHEDULED_TIME,
        current_role_id: Optional[int] = None,
        current_thresh: int = 3,
        lang: Optional[str] = None,
    ):
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.current_time = current_time
        self.current_role_id = current_role_id
        self.current_thresh = current_thresh
        self.lang = lang or qotd.get_user_language(None, guild_id)

        discord_ch = qotd.bot.get_channel(channel_id)
        ch_name = discord_ch.name if discord_ch else str(channel_id)
        super().__init__(title=f"{t(self.lang, 'btn_settings')}: #{ch_name}"[:45])

        # 1. Schedule Time (TextInput)
        self.schedule_input = discord.ui.TextInput(
            label=t(self.lang, "modal_time_label")[:45],
            placeholder=t(self.lang, "modal_time_placeholder")[:100],
            default=current_time,
            min_length=5,
            max_length=5,
            required=True,
        )
        self.add_item(self.schedule_input)

        # 2. Ping Role (RoleSelect)
        role_default = [discord.Object(id=current_role_id)] if current_role_id else discord.utils.MISSING
        self.role_select = discord.ui.RoleSelect(
            placeholder=t(self.lang, "select_ping_role")[:100],
            min_values=0,
            max_values=1,
            required=False,
            default_values=role_default,
        )
        self.role_label = discord.ui.Label(
            text=t(self.lang, "btn_manage_channel_role")[:45],
            component=self.role_select,
        )
        self.add_item(self.role_label)

        # 3. Low Queue Threshold (TextInput)
        self.threshold_input = discord.ui.TextInput(
            label=t(self.lang, "modal_threshold_label")[:45],
            default=str(current_thresh),
            placeholder="3",
            min_length=1,
            max_length=2,
            required=True,
        )
        self.add_item(self.threshold_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        # 1. Validate Schedule Time
        scheduled_time = str(self.schedule_input).strip()
        if not re.fullmatch(r"^(?:[01]\d|2[0-3]):[0-5]\d$", scheduled_time):
            await interaction.response.send_message(t(user_lang, "time_invalid"), ephemeral=True)
            return

        # 2. Validate Threshold
        raw_thresh = str(self.threshold_input).strip()
        if not raw_thresh.isdigit() or not (1 <= int(raw_thresh) <= 50):
            await interaction.response.send_message("⚠️ Please enter a threshold between 1 and 50.", ephemeral=True)
            return
        low_queue_threshold = int(raw_thresh)

        # 3. Resolve Ping Role
        role_id: Optional[int] = None
        if self.role_select.values:
            try:
                val = self.role_select.values[0]
                role_id = int(val.id) if hasattr(val, "id") else int(val)
            except (IndexError, ValueError, TypeError):
                role_id = None

        # 4. Schedule posting date logic
        now_local = datetime.now(QOTD_TIMEZONE)
        today = now_local.date().isoformat()
        current_minute = now_local.time().replace(second=0, microsecond=0)
        last_posted = None
        try:
            s_time = datetime.strptime(scheduled_time, "%H:%M").time()
            if s_time < current_minute:
                last_posted = today
        except (ValueError, TypeError):
            pass

        await self.qotd.update_qotd_channel(
            self.channel_id,
            scheduled_time=scheduled_time,
            role_id=role_id,
            low_queue_threshold=low_queue_threshold,
            last_posted_date=last_posted,
        )

        embed = await self.qotd.build_settings_embed(self.guild_id, lang=user_lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        from .views import QotdPanelView
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=user_lang)

        msg_content = t(user_lang, "setup_saved")
        if interaction.message and not interaction.response.is_done():
            await interaction.response.edit_message(content=msg_content, embed=embed, view=view)
        elif not interaction.response.is_done():
            await interaction.response.send_message(msg_content, ephemeral=True)
        else:
            await interaction.followup.send(msg_content, ephemeral=True)


def find_component_data(components: Sequence[Dict[str, Any]], target_custom_id: str) -> Optional[Dict[str, Any]]:
    for comp in components:
        if comp.get("type") == 1:
            res = find_component_data(comp.get("components", []), target_custom_id)
            if res is not None:
                return res
        elif comp.get("type") == 18:
            inner = comp.get("component")
            if inner and isinstance(inner, dict):
                if inner.get("custom_id") == target_custom_id:
                    return inner
                res = find_component_data([inner], target_custom_id)
                if res is not None:
                    return res
        elif comp.get("custom_id") == target_custom_id:
            return comp
    return None


class SingleCheckbox(discord.ui.CheckboxGroup):
    """A CheckboxGroup configured as a single checkbox with full support for required=True and .value boolean access."""

    def __init__(
        self,
        *,
        label: str,
        value: str,
        required: bool = False,
        default: bool = False,
        custom_id: str = discord.utils.MISSING,
    ):
        super().__init__(
            custom_id=custom_id,
            required=required,
            min_values=1 if required else 0,
            max_values=1,
            options=[
                discord.CheckboxGroupOption(
                    label=label[:100],
                    value=value,
                    default=default,
                )
            ],
        )
        self._target_val = value
        self._custom_value: Optional[bool] = None

    @property
    def value(self) -> bool:
        if self._custom_value is not None:
            return bool(self._custom_value)
        return self._target_val in self.values

    @value.setter
    def value(self, val: bool):
        self._custom_value = val
        if val:
            self._values = [self._target_val]
        else:
            self._values = []

    @property
    def _value(self) -> bool:
        return self.value

    @_value.setter
    def _value(self, val: bool):
        self.value = val

    def _handle_submit(self, interaction, data, resolved):
        super()._handle_submit(interaction, data, resolved)
        val = data.get("value")
        if val is True or val == "true" or val == 1:
            self._values = [self._target_val]
        elif val is False or val == "false" or val == 0:
            self._values = []


class UnlinkChannelModal(BaseModal):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        channels: Optional[List[Dict[str, Any]]] = None,
        target_channel_id: Optional[int] = None,
        lang: Optional[str] = None,
    ):
        self.qotd = qotd
        self.guild_id = guild_id
        self.channels = channels or []
        self.target_channel_id = target_channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)
        super().__init__(title=t(self.lang, "modal_unlink_title")[:45])

        # 1. Channel Select (Select menu of active QOTD channels)
        options = []
        for i, c in enumerate(self.channels[:25]):
            cid = c["channel_id"]
            ch_obj = qotd.bot.get_channel(cid)
            c_name = ch_obj.name if ch_obj else str(cid)
            is_default = (cid == target_channel_id) if target_channel_id else (i == 0)
            options.append(discord.SelectOption(label=f"#{c_name}"[:100], value=str(cid), default=is_default))

        if not options:
            options = [discord.SelectOption(label="No channels", value="0")]

        self.channel_select = discord.ui.Select(
            placeholder=t(self.lang, "select_channel_manage_placeholder")[:100],
            options=options,
            min_values=1,
            max_values=1,
            required=True,
        )
        self.channel_label = discord.ui.Label(
            text=t(self.lang, "channel_label")[:45],
            component=self.channel_select,
        )
        self.add_item(self.channel_label)

        # 2. Delete Questions Checkbox (REQUIRED: min_values=1, required=True)
        self.delete_questions_cb = SingleCheckbox(
            label=t(self.lang, "unlink_delete_questions_option")[:100],
            value="delete",
            required=True,
            default=False,
        )
        self.delete_questions_label = discord.ui.Label(
            text=t(self.lang, "btn_delete_questions")[:45],
            description=t(self.lang, "unlink_delete_warning")[:100],
            component=self.delete_questions_cb,
        )
        self.add_item(self.delete_questions_label)

        # 3. Are you sure? Checkbox (REQUIRED: min_values=1, required=True)
        self.confirm_cb = SingleCheckbox(
            label=t(self.lang, "unlink_confirm_checkbox_label")[:100],
            value="confirm",
            required=True,
            default=False,
        )
        self.confirm_label = discord.ui.Label(
            text=t(self.lang, "confirmation_label")[:45],
            component=self.confirm_cb,
        )
        self.add_item(self.confirm_label)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang = self.qotd.get_user_language(interaction, self.guild_id)
        if not is_admin(interaction):
            await interaction.response.send_message(t(user_lang, "admin_only"), ephemeral=True)
            return

        raw_components = interaction.data.get("components", []) if interaction.data else []

        # Check delete_questions confirmation: must be confirmed
        delete_confirmed = bool(self.delete_questions_cb.value)
        if not delete_confirmed and raw_components:
            raw_del = find_component_data(raw_components, self.delete_questions_cb.custom_id)
            if raw_del:
                val = raw_del.get("value")
                vals = raw_del.get("values", [])
                if val in (True, "true", "1", 1) or "delete" in vals or "true" in vals:
                    delete_confirmed = True

        if not delete_confirmed:
            await interaction.response.send_message(
                f"{Icon.CANCEL} " + t(user_lang, "unlink_delete_required"),
                ephemeral=True,
            )
            return

        # Check confirm unlinking: must be confirmed
        is_confirmed = bool(self.confirm_cb.value)
        if not is_confirmed and raw_components:
            raw_confirm = find_component_data(raw_components, self.confirm_cb.custom_id)
            if raw_confirm:
                val = raw_confirm.get("value")
                vals = raw_confirm.get("values", [])
                if val in (True, "true", "1", 1) or "confirm" in vals or "true" in vals:
                    is_confirmed = True

        if not is_confirmed:
            await interaction.response.send_message(
                f"{Icon.CANCEL} " + t(user_lang, "unlink_not_confirmed"),
                ephemeral=True,
            )
            return

        if not self.channel_select.values or self.channel_select.values[0] == "0":
            await interaction.response.send_message(t(user_lang, "not_found"), ephemeral=True)
            return

        try:
            val = self.channel_select.values[0]
            channel_id = int(val.value) if hasattr(val, "value") else int(val)
        except (ValueError, TypeError, IndexError):
            await interaction.response.send_message(t(user_lang, "not_found"), ephemeral=True)
            return

        # Deletion of questions is required and not optional
        await self.qotd.delete_qotd_channel(channel_id, delete_questions=True)

        embed = await self.qotd.build_settings_embed(self.guild_id, lang=user_lang)
        channels = await self.qotd.get_qotd_channels(self.guild_id)
        from .views import QotdPanelView
        view = QotdPanelView(self.qotd, section="settings", guild_id=self.guild_id, channels=channels, lang=user_lang)

        msg_content = t(user_lang, "channel_unlinked", channel_id=channel_id)
        if interaction.message and not interaction.response.is_done():
            await interaction.response.edit_message(content=msg_content, embed=embed, view=view)
        elif not interaction.response.is_done():
            await interaction.response.send_message(msg_content, ephemeral=True)
        else:
            await interaction.followup.send(msg_content, ephemeral=True)


# ---------------------------------------------------------------------------
# Dynamic Review Button (Persistent admin channel notification)
# ---------------------------------------------------------------------------


