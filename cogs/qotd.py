import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

DATABASE_FILE = "qotd.db"
LEGACY_SETTINGS_FILE = "qotd_settings.json"
DEFAULT_QOTD_HOUR = 9
DEFAULT_SCHEDULED_TIME = "09:00"
QOTD_TIMEZONE = ZoneInfo("Europe/Prague")
REVIEW_CUSTOM_ID = r"^qotd:review:(accept|decline|edit):(\d+):(.+)$"

# Storage & abuse safety limits
MAX_QUEUE_QUESTIONS = 500          # Max active questions waiting in queue per channel (~1.5 years of daily QOTD)
MAX_TOTAL_QUESTIONS = 2500         # Max total questions (queue + history) per server
MAX_PENDING_SUGGESTIONS = 100      # Max pending suggestions in server review mailbox
MAX_USER_PENDING_SUGGESTIONS = 3   # Max pending suggestions per individual user
MAX_QUESTION_LENGTH = 300          # Max character length for any single question
MAX_BATCH_ADD_QUESTIONS = 50       # Max questions allowed in a single bulk paste

# Brand colors for clean visual distinction
COLOR_QUEUE = discord.Colour(16760576)                  # Amber / Gold
COLOR_SUGGESTIONS = discord.Colour.from_str("#FEE75C")  # Yellow
COLOR_HISTORY = discord.Colour.from_str("#57F287")      # Green
COLOR_SETTINGS = discord.Colour.from_str("#9B59B6")     # Purple
COLOR_LEADERBOARD = discord.Colour.from_str("#F1C40F")  # Gold
COLOR_DETAIL = discord.Colour.from_str("#3498DB")       # Blue
COLOR_DANGER = discord.Colour.from_str("#ED4245")       # Red
COLOR_POST = discord.Colour.from_str("#387b44")         # Forest Green

logger = logging.getLogger("qotd")

from translations import (
    DEFAULT_LANGUAGE,
    DEFAULT_QUESTIONS,
    SUPPORTED_LANGUAGES,
    t,
    resolve_user_locale,
)


def normalize_question(text: str) -> str:
    """Normalizes text for robust duplicate comparison (strips numbers, bullets, whitespace, case)."""
    cleaned = re.sub(r"^\s*(?:\d+[\.\)]|[-*•])\s*", "", text)
    cleaned = cleaned.strip().strip("\"'").lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def parse_question_input(text: str) -> List[str]:
    """Splits multiline input into individual questions, stripping empty lines and bullets."""
    lines = [line.strip() for line in text.splitlines()]
    questions = []
    for line in lines:
        if not line:
            continue
        cleaned = re.sub(r"^\s*(?:\d+[\.\)]|[-*•])\s*", "", line).strip()
        if cleaned:
            questions.append(cleaned)
    return questions


def format_discord_timestamp(iso_str: Optional[str], format_type: str = "f", lang: str = "en") -> str:
    if not iso_str:
        return t(lang, "unknown_date")
    try:
        dt = datetime.fromisoformat(iso_str)
        return f"<t:{int(dt.timestamp())}:{format_type}>"
    except Exception:
        return iso_str[:10]


def is_admin(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )


# ---------------------------------------------------------------------------
# Base classes for resilient error & timeout handling
# ---------------------------------------------------------------------------

class BaseModal(discord.ui.Modal):
    """Base modal that logs errors and sends an ephemeral error message to the user
    instead of leaving the modal spinner hanging indefinitely."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Error in modal %s: %s", self.__class__.__name__, error)
        msg = "❌ An unexpected error occurred while processing your input."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass


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
        if cur_queue >= MAX_QUEUE_QUESTIONS:
            await interaction.followup.send(
                t(user_lang, "limit_queue_full", max_q=MAX_QUEUE_QUESTIONS),
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

        remaining_slots = min(MAX_QUEUE_QUESTIONS - cur_queue, MAX_TOTAL_QUESTIONS - cur_total)
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
            lines.append(t(user_lang, "skipped_capacity", count=skipped_capacity, max_q=MAX_QUEUE_QUESTIONS, max_total=MAX_TOTAL_QUESTIONS))

        msg_text = "\n".join(lines) if lines else t(user_lang, "no_new_questions_added")
        await interaction.followup.send(msg_text, ephemeral=True)

        channels = await self.qotd.get_qotd_channels(self.guild_id)
        embed = await self.qotd.build_questions_embed(self.guild_id, "to_ask", self.current_page, channel_id=self.channel_id, lang=user_lang)
        total_pages = await self.qotd.question_page_count(self.guild_id, "to_ask", channel_id=self.channel_id)
        page_items = await self.qotd.get_questions_page(self.guild_id, "to_ask", self.current_page, channel_id=self.channel_id)
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
            view = QuestionDetailView(self.qotd, self.guild_id, updated_row, return_section=self.return_section, return_page=self.return_page, channel_id=self.channel_id, lang=user_lang)
            await interaction.edit_original_response(embed=embed, view=view)


class EditSuggestionModal(BaseModal):
    def __init__(
        self,
        qotd: "Qotd",
        guild_id: int,
        suggestion_id: str,
        current_text: str,
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
        super().__init__(title=t(self.lang, "modal_edit_sugg_title"))

        self.question_input = discord.ui.TextInput(
            label=t(self.lang, "modal_edit_sugg_label"),
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

        if self.from_panel:
            ok, reason = await self.qotd.accept_suggestion(
                self.guild_id,
                self.suggestion_id,
                question=new_text,
                review_message=None,
                added_by=interaction.user,
                interaction=None,
            )
            if ok:
                await interaction.followup.send(t(user_lang, "sugg_approved_msg", question=new_text), ephemeral=True)
                total_pages = await self.qotd.suggestions_page_count(self.guild_id)
                target_page = min(self.return_page, max(0, total_pages - 1))
                embed = await self.qotd.build_suggestions_embed(self.guild_id, target_page, lang=user_lang)
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
                    lang=user_lang,
                )
                try:
                    await interaction.edit_original_response(embed=embed, view=view)
                except Exception:
                    pass
            else:
                if reason == "queue_full":
                    await interaction.followup.send(t(user_lang, "sugg_cannot_approve_queue", max_q=MAX_QUEUE_QUESTIONS), ephemeral=True)
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
            )
            if ok:
                await interaction.followup.send(t(user_lang, "sugg_approved_msg", question=new_text), ephemeral=True)
            else:
                if reason == "queue_full":
                    await interaction.followup.send(t(user_lang, "sugg_cannot_approve_queue", max_q=MAX_QUEUE_QUESTIONS), ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)

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
            if ok:
                await interaction.followup.send(t(user_lang, "sugg_rejected_msg"), ephemeral=True)
            else:
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
            source_icon = "💡" if row["source"] == "suggestion" else ("👤" if row["source"] == "manual" else "🤖")
            status_text = t(user_lang, "status_queue") if row["status"] == "to_ask" else t(user_lang, "status_asked")
            embed.add_field(
                name=f"{idx}. [#{row['id']}] {status_text} • {source_icon}",
                value=row["question"][:150],
                inline=False,
            )

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
        except Exception:
            await self.qotd.update_qotd_channel(self.channel_id, scheduled_time=scheduled_time)

        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=user_lang)
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
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=user_lang)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(t(user_lang, "threshold_updated", threshold=val), ephemeral=True)


# ---------------------------------------------------------------------------
# Dynamic Review Button (Persistent admin channel notification)
# ---------------------------------------------------------------------------

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
            await interaction.response.defer(ephemeral=True)
            ok, reason = await qotd_cog.accept_suggestion(
                self.guild_id,
                self.suggestion_id,
                review_message=interaction.message,
                added_by=interaction.user,
                interaction=interaction,
            )
            if ok:
                await interaction.followup.send(t(admin_lang, "sugg_approved_short"), ephemeral=True)
            else:
                if reason == "queue_full":
                    await interaction.followup.send(t(admin_lang, "sugg_cannot_approve_queue", max_q=MAX_QUEUE_QUESTIONS), ephemeral=True)
                elif reason == "total_limit":
                    await interaction.followup.send(t(admin_lang, "sugg_cannot_approve_total", max_total=MAX_TOTAL_QUESTIONS), ephemeral=True)
                else:
                    await interaction.followup.send(t(admin_lang, "sugg_approve_failed"), ephemeral=True)

        elif self.action == "decline":
            modal = RejectSuggestionModal(qotd_cog, self.guild_id, self.suggestion_id, lang=admin_lang)
            await interaction.response.send_modal(modal)

        elif self.action == "edit":
            cursor = await qotd_cog.db.execute(
                "SELECT question FROM suggestions WHERE id = ? AND guild_id = ?",
                (self.suggestion_id, self.guild_id),
            )
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message(t(admin_lang, "not_found"), ephemeral=True)
                return
            modal = EditSuggestionModal(qotd_cog, self.guild_id, self.suggestion_id, row[0], lang=admin_lang)
            await interaction.response.send_modal(modal)


def suggestion_review_view(guild_id: int, suggestion_id: str, lang: str = "en") -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    b_acc = discord.ui.Button(
        label=t(lang, "btn_approve"),
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=f"qotd:review:accept:{guild_id}:{suggestion_id}",
    )
    b_dec = discord.ui.Button(
        label=t(lang, "btn_decline"),
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id=f"qotd:review:decline:{guild_id}:{suggestion_id}",
    )
    b_edit = discord.ui.Button(
        label=t(lang, "btn_edit_approve"),
        style=discord.ButtonStyle.primary,
        emoji="✏️",
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
        emoji="💡",
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
        emoji="ℹ️",
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
                emoji="📌",
                default=(selected == "to_ask"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_suggestions"),
                value="suggestions",
                emoji="💡",
                default=(selected == "suggestions"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_asked"),
                value="asked",
                emoji="📜",
                default=(selected == "asked"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_settings"),
                value="settings",
                emoji="⚙️",
                default=(selected == "settings"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_top"),
                value="top",
                emoji="🏆",
                default=(selected == "top"),
            ),
            discord.SelectOption(
                label=t(lang, "sec_info"),
                value="info",
                emoji="ℹ️",
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
            btn_add = discord.ui.Button(label=t(self.lang, "btn_add_questions"), style=discord.ButtonStyle.success, emoji="➕", row=3)
            btn_add.callback = self.on_add_questions
            self.add_item(btn_add)

            btn_search = discord.ui.Button(label=t(self.lang, "btn_search"), style=discord.ButtonStyle.secondary, emoji="🔍", row=3)
            btn_search.callback = self.on_search
            self.add_item(btn_search)

            if self.page_items:
                btn_remove = discord.ui.Button(label=t(self.lang, "btn_bulk_delete"), style=discord.ButtonStyle.danger, emoji="🗑️", row=3)
                btn_remove.callback = self.on_bulk_delete
                self.add_item(btn_remove)

                btn_send = discord.ui.Button(label=t(self.lang, "btn_send_random"), style=discord.ButtonStyle.primary, emoji="🎲", row=3)
                btn_send.callback = self.on_send_random
                self.add_item(btn_send)

        elif section == "asked":
            btn_search = discord.ui.Button(label=t(self.lang, "btn_search"), style=discord.ButtonStyle.secondary, emoji="🔍", row=3)
            btn_search.callback = self.on_search
            self.add_item(btn_search)

            if self.page_items:
                btn_remove = discord.ui.Button(label=t(self.lang, "btn_bulk_delete"), style=discord.ButtonStyle.danger, emoji="🗑️", row=3)
                btn_remove.callback = self.on_bulk_delete
                self.add_item(btn_remove)

                btn_clear = discord.ui.Button(label=t(self.lang, "btn_clear_category"), style=discord.ButtonStyle.secondary, emoji="⚠️", row=3)
                btn_clear.callback = self.on_clear_category
                self.add_item(btn_clear)

        elif section == "suggestions" and self.page_items:
            btn_approve_all = discord.ui.Button(label=t(self.lang, "btn_approve_all"), style=discord.ButtonStyle.success, emoji="✅", row=2)
            btn_approve_all.callback = self.on_approve_all
            self.add_item(btn_approve_all)

            btn_clear_sugg = discord.ui.Button(label=t(self.lang, "btn_clear_category"), style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
            btn_clear_sugg.callback = self.on_clear_category
            self.add_item(btn_clear_sugg)

        elif section == "settings":
            btn_add_chan = discord.ui.Button(label=t(self.lang, "btn_add_qotd_channel"), style=discord.ButtonStyle.success, emoji="➕", row=2)
            btn_add_chan.callback = self.on_add_channel_click
            self.add_item(btn_add_chan)

            btn_admin_chan = discord.ui.Button(label=t(self.lang, "settings_admin_channel"), style=discord.ButtonStyle.secondary, emoji="🛡️", row=2)
            btn_admin_chan.callback = self.on_admin_channel_click
            self.add_item(btn_admin_chan)

            btn_lang = discord.ui.Button(label=t(self.lang, "btn_change_language"), style=discord.ButtonStyle.secondary, emoji="🌐", row=2)
            btn_lang.callback = self.on_change_language
            self.add_item(btn_lang)

        # Row 4: Pagination & Refresh Controls
        btn_row = 4 if section in ("to_ask", "asked") else 3
        if section in ("to_ask", "asked", "suggestions") and self.total_pages > 1:
            btn_first = discord.ui.Button(emoji="⏮️", style=discord.ButtonStyle.secondary, disabled=(self.page == 0), row=btn_row)
            btn_first.callback = self.on_page_first
            self.add_item(btn_first)

            btn_prev = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.primary, disabled=(self.page == 0), row=btn_row)
            btn_prev.callback = self.on_page_prev
            self.add_item(btn_prev)

            btn_next = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.primary, disabled=(self.page >= self.total_pages - 1), row=btn_row)
            btn_next.callback = self.on_page_next
            self.add_item(btn_next)

            btn_last = discord.ui.Button(emoji="⏭️", style=discord.ButtonStyle.secondary, disabled=(self.page >= self.total_pages - 1), row=btn_row)
            btn_last.callback = self.on_page_last
            self.add_item(btn_last)

        btn_refresh = discord.ui.Button(label=t(self.lang, "btn_refresh"), style=discord.ButtonStyle.secondary, emoji="🔄", row=btn_row)
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
        await interaction.response.defer()
        selected_id = int(interaction.data["values"][0])
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, selected_id, lang=self.lang)
        view = ChannelManageView(self.qotd, self.guild_id, selected_id, lang=self.lang)
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_add_channel_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = AddQotdChannelSelectView(self.qotd, self.guild_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "btn_add_qotd_channel"),
            description="Select a text channel from the dropdown below to register it as an active QOTD channel.",
            colour=COLOR_SETTINGS,
        )
        await interaction.edit_original_response(embed=embed, view=view)

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
        modal = AddQuestionModal(self.qotd, self.guild_id, current_page=self.page, channel_id=self.channel_id, lang=self.lang)
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
        await interaction.response.defer(ephemeral=True)
        cur_queue = await self.qotd.get_queue_count(self.guild_id)
        cur_total = await self.qotd.get_total_question_count(self.guild_id)
        if cur_queue >= MAX_QUEUE_QUESTIONS:
            await interaction.followup.send(t(self.lang, "sugg_cannot_approve_queue", max_q=MAX_QUEUE_QUESTIONS), ephemeral=True)
            return
        if cur_total >= MAX_TOTAL_QUESTIONS:
            await interaction.followup.send(t(self.lang, "sugg_cannot_approve_total", max_total=MAX_TOTAL_QUESTIONS), ephemeral=True)
            return

        cursor = await self.qotd.db.execute("SELECT * FROM suggestions WHERE guild_id = ? ORDER BY created_at ASC", (self.guild_id,))
        all_suggs = await cursor.fetchall()
        if not all_suggs:
            await interaction.followup.send(t(self.lang, "no_pending_suggestions"), ephemeral=True)
            return

        approved_count = 0
        for s in all_suggs:
            ok, _ = await self.qotd.accept_suggestion(self.guild_id, s["id"], added_by=interaction.user)
            if ok:
                approved_count += 1
            else:
                break

        await interaction.followup.send(t(self.lang, "approved_all_msg", count=approved_count), ephemeral=True)
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

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
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

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
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

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
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
            content=f"✅ Admin review channel set to <#{channel_id}>.",
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

        btn_hour = discord.ui.Button(label=t(self.lang, "btn_manage_channel_hour"), style=discord.ButtonStyle.primary, emoji="⏰", row=0)
        btn_hour.callback = self.on_hour
        self.add_item(btn_hour)

        btn_role = discord.ui.Button(label=t(self.lang, "btn_manage_channel_role"), style=discord.ButtonStyle.secondary, emoji="🔔", row=0)
        btn_role.callback = self.on_role
        self.add_item(btn_role)

        btn_thresh = discord.ui.Button(label=t(self.lang, "btn_manage_channel_threshold"), style=discord.ButtonStyle.secondary, emoji="⚠️", row=0)
        btn_thresh.callback = self.on_threshold
        self.add_item(btn_thresh)

        btn_unlink = discord.ui.Button(label=t(self.lang, "btn_unlink_channel"), style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
        btn_unlink.callback = self.on_unlink
        self.add_item(btn_unlink)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

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

    async def on_unlink(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = UnlinkChannelConfirmView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
        embed = discord.Embed(
            title=t(self.lang, "unlink_confirm_title"),
            description=t(self.lang, "unlink_confirm_desc", channel_id=self.channel_id),
            colour=COLOR_DANGER,
        )
        await interaction.edit_original_response(embed=embed, view=view)

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

        btn_clear = discord.ui.Button(label="Remove Role Mention", style=discord.ButtonStyle.danger, emoji="🚫", row=1)
        btn_clear.callback = self.on_clear_role
        self.add_item(btn_clear)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
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
        await interaction.edit_original_response(content=f"✅ Role updated to <@&{role_id}>.", embed=embed, view=view)

    async def on_clear_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.qotd.update_qotd_channel(self.channel_id, role_id=None)
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=self.lang)
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
        await interaction.edit_original_response(content="✅ Role mention removed.", embed=embed, view=view)

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=self.lang)
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
        await interaction.edit_original_response(content=None, embed=embed, view=view)


class UnlinkChannelConfirmView(BaseTimeoutView):
    def __init__(self, qotd: "Qotd", guild_id: int, channel_id: int, lang: Optional[str] = None):
        super().__init__(timeout=180)
        self.qotd = qotd
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.lang = lang or qotd.get_user_language(None, guild_id)

        btn_keep = discord.ui.Button(label=t(self.lang, "btn_keep_questions"), style=discord.ButtonStyle.secondary, emoji="📦", row=0)
        btn_keep.callback = self.on_keep
        self.add_item(btn_keep)

        btn_del = discord.ui.Button(label=t(self.lang, "btn_delete_questions"), style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
        btn_del.callback = self.on_delete
        self.add_item(btn_del)

        btn_cancel = discord.ui.Button(label=t(self.lang, "btn_cancel"), style=discord.ButtonStyle.secondary, emoji="↩️", row=0)
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
        embed = await self.qotd.build_channel_manage_embed(self.guild_id, self.channel_id, lang=self.lang)
        view = ChannelManageView(self.qotd, self.guild_id, self.channel_id, lang=self.lang)
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

        btn_edit = discord.ui.Button(label=t(self.lang, "btn_edit_text"), style=discord.ButtonStyle.primary, emoji="✏️", row=0)
        btn_edit.callback = self.on_edit
        self.add_item(btn_edit)

        if self.status == "to_ask":
            btn_send = discord.ui.Button(label=t(self.lang, "btn_send_now"), style=discord.ButtonStyle.success, emoji="⚡", row=0)
            btn_send.callback = self.on_send_now
            self.add_item(btn_send)
        else:
            btn_requeue = discord.ui.Button(label=t(self.lang, "btn_requeue"), style=discord.ButtonStyle.success, emoji="🔄", row=0)
            btn_requeue.callback = self.on_requeue
            self.add_item(btn_requeue)

        btn_del = discord.ui.Button(label=t(self.lang, "btn_delete"), style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
        btn_del.callback = self.on_delete
        self.add_item(btn_del)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=0)
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
        cur_queue = await self.qotd.get_queue_count(self.guild_id, self.channel_id)
        if cur_queue >= MAX_QUEUE_QUESTIONS:
            await interaction.followup.send(t(self.lang, "requeue_queue_full", max_q=MAX_QUEUE_QUESTIONS), ephemeral=True)
            return
        if await self.qotd.requeue_question(self.guild_id, self.question_id, channel_id=self.channel_id):
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
            emoji="↩️",
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

        btn_approve = discord.ui.Button(label=t(self.lang, "btn_approve"), style=discord.ButtonStyle.success, emoji="✅", row=0)
        btn_approve.callback = self.on_approve
        self.add_item(btn_approve)

        btn_edit = discord.ui.Button(label=t(self.lang, "btn_edit_approve"), style=discord.ButtonStyle.primary, emoji="✏️", row=0)
        btn_edit.callback = self.on_edit
        self.add_item(btn_edit)

        btn_decline = discord.ui.Button(label=t(self.lang, "btn_decline"), style=discord.ButtonStyle.danger, emoji="❌", row=0)
        btn_decline.callback = self.on_decline
        self.add_item(btn_decline)

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=0)
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message(t(self.lang, "admin_only"), ephemeral=True)
            return False
        return True

    async def on_approve(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, reason = await self.qotd.accept_suggestion(self.guild_id, self.suggestion_id, added_by=interaction.user)
        if ok:
            await interaction.followup.send(t(self.lang, "sugg_approved_msg", question=self.suggestion_row['question']), ephemeral=True)
            await self.on_back(interaction)
        else:
            if reason == "queue_full":
                await interaction.followup.send(t(self.lang, "sugg_cannot_approve_queue", max_q=MAX_QUEUE_QUESTIONS), ephemeral=True)
            elif reason == "total_limit":
                await interaction.followup.send(t(self.lang, "sugg_cannot_approve_total", max_total=MAX_TOTAL_QUESTIONS), ephemeral=True)
            else:
                await interaction.followup.send(t(self.lang, "sugg_approve_failed"), ephemeral=True)

    async def on_edit(self, interaction: discord.Interaction):
        modal = EditSuggestionModal(self.qotd, self.guild_id, self.suggestion_id, self.suggestion_row["question"], return_page=self.return_page, from_panel=True, lang=self.lang)
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

        btn_confirm = discord.ui.Button(label=t(self.lang, "btn_bulk_delete"), style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
        btn_confirm.callback = self.on_confirm_selected
        self.add_item(btn_confirm)

        btn_cancel = discord.ui.Button(label=t(self.lang, "btn_cancel"), style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
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

        btn_back = discord.ui.Button(label=t(self.lang, "btn_back"), style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
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

        btn_confirm = discord.ui.Button(label=t(self.lang, "btn_yes_delete_all"), emoji="🗑️", style=discord.ButtonStyle.danger)
        btn_confirm.callback = self.confirm
        self.add_item(btn_confirm)

        btn_cancel = discord.ui.Button(label=t(self.lang, "btn_cancel"), emoji="↩️", style=discord.ButtonStyle.secondary)
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

class Qotd(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: Optional[aiosqlite.Connection] = None
        self.guild_languages: Dict[int, str] = {}
        self._channel_post_locks: Dict[int, asyncio.Lock] = {}

    def _get_channel_lock(self, channel_id: int) -> asyncio.Lock:
        """Get or create a per-channel asyncio.Lock for question dispatch."""
        if channel_id not in self._channel_post_locks:
            self._channel_post_locks[channel_id] = asyncio.Lock()
        return self._channel_post_locks[channel_id]

    async def cog_load(self):
        self.db = await aiosqlite.connect(DATABASE_FILE)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.initialize_database()
        self.bot.add_dynamic_items(SuggestionReviewButton)
        for lang_code in SUPPORTED_LANGUAGES:
            self.bot.add_view(QotdView(self, lang=lang_code))
        self.qotd_scheduler.start()

    async def cog_unload(self):
        self.qotd_scheduler.cancel()
        if self.db:
            await self.db.close()

    def get_server_language(self, guild_id: Optional[int]) -> str:
        """Returns the configured server language, defaulting to English."""
        if not guild_id:
            return DEFAULT_LANGUAGE
        return self.guild_languages.get(guild_id, DEFAULT_LANGUAGE)

    def get_user_language(self, interaction: Optional[discord.Interaction], guild_id: Optional[int] = None) -> str:
        """
        Resolves the language for an individual user:
        1. User's client language (interaction.locale) if supported.
        2. Fallback to server's configured language.
        3. Fallback to DEFAULT_LANGUAGE ('en').
        """
        if interaction and hasattr(interaction, "locale") and interaction.locale:
            user_lang = resolve_user_locale(interaction.locale)
            if user_lang:
                return user_lang
        effective_guild_id = guild_id or (interaction.guild_id if interaction else None)
        return self.get_server_language(effective_guild_id)

    def get_language(self, guild_id: Optional[int]) -> str:
        """Backward-compatible alias for get_server_language."""
        return self.get_server_language(guild_id)

    get_guild_language = get_language

    async def set_guild_language(self, guild_id: int, lang: str):
        if lang not in SUPPORTED_LANGUAGES:
            lang = DEFAULT_LANGUAGE
        self.guild_languages[guild_id] = lang
        await self.update_guild_settings(guild_id, language=lang)

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

        sugg_cols = await get_cols("suggestions")
        if "target_channel_id" not in sugg_cols:
            await self.db.execute("ALTER TABLE suggestions ADD COLUMN target_channel_id INTEGER DEFAULT NULL")
        if "language" not in sugg_cols:
            await self.db.execute("ALTER TABLE suggestions ADD COLUMN language TEXT DEFAULT NULL")

        chan_cols = await get_cols("qotd_channels")
        if "last_thread_id" not in chan_cols:
            await self.db.execute("ALTER TABLE qotd_channels ADD COLUMN last_thread_id INTEGER DEFAULT NULL")
        if "qotd_number" not in chan_cols:
            await self.db.execute("ALTER TABLE qotd_channels ADD COLUMN qotd_number INTEGER NOT NULL DEFAULT 0")

        settings_cols = await get_cols("settings")
        if "language" not in settings_cols:
            await self.db.execute("ALTER TABLE settings ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")

        # Indexes
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_questions_guild_status ON questions(guild_id, status)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_questions_channel_status ON questions(guild_id, channel_id, status)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_questions_guild_source ON questions(guild_id, source, suggested_by_id)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_guild ON suggestions(guild_id)")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_guild_user ON suggestions(guild_id, user_id)")

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
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor = await self.db.execute(
            """
            INSERT INTO qotd_channels (
                guild_id, channel_id, role_id, scheduled_time, low_queue_threshold, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, role_id, scheduled_time, low_queue_threshold, now_iso),
        )
        await self.db.commit()

        # Seed starter questions specifically for this new channel
        server_lang = self.get_server_language(guild_id)
        await self.seed_default_questions(guild_id, channel_id=channel_id, lang=server_lang)
        return cursor.lastrowid

    _VALID_CHANNEL_COLS = frozenset({
        "role_id", "scheduled_time", "low_queue_threshold",
        "last_posted_date", "last_thread_id", "qotd_number",
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
        await self.db.execute("DELETE FROM qotd_channels WHERE channel_id = ?", (channel_id,))
        if delete_questions:
            await self.db.execute("DELETE FROM questions WHERE channel_id = ?", (channel_id,))
            await self.db.execute("DELETE FROM suggestions WHERE target_channel_id = ?", (channel_id,))
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
            }
        return dict(row)

    _VALID_SETTINGS_COLS = frozenset({
        "admin_channel_id", "language",
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
        if channel_id:
            cursor = await self.db.execute("SELECT question FROM questions WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
            existing_rows = await cursor.fetchall()
            cursor_sugg = await self.db.execute("SELECT question FROM suggestions WHERE guild_id = ? AND target_channel_id = ?", (guild_id, channel_id))
            existing_sugg = await cursor_sugg.fetchall()
        else:
            cursor = await self.db.execute("SELECT question FROM questions WHERE guild_id = ?", (guild_id,))
            existing_rows = await cursor.fetchall()
            cursor_sugg = await self.db.execute("SELECT question FROM suggestions WHERE guild_id = ?", (guild_id,))
            existing_sugg = await cursor_sugg.fetchall()

        existing_norm = {normalize_question(r["question"]) for r in existing_rows}
        existing_norm.update(normalize_question(r["question"]) for r in existing_sugg)

        unique = []
        duplicates = []
        for q in candidate_questions:
            norm = normalize_question(q)
            if not norm:
                continue
            if norm in existing_norm:
                duplicates.append(q)
            else:
                existing_norm.add(norm)
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
    ) -> Optional[int]:
        question_clean = question.strip()
        if not question_clean:
            return None
        if len(question_clean) > MAX_QUESTION_LENGTH:
            question_clean = question_clean[:MAX_QUESTION_LENGTH]

        if not bypass_queue_limit:
            queue_count = await self.get_queue_count(guild_id, channel_id=channel_id)
            if queue_count >= MAX_QUEUE_QUESTIONS:
                logger.warning("Queue limit reached for guild %s channel %s (%d/%d)", guild_id, channel_id, queue_count, MAX_QUEUE_QUESTIONS)
                return None

        total_count = await self.get_total_question_count(guild_id)
        if total_count >= MAX_TOTAL_QUESTIONS:
            logger.warning("Total questions limit reached for guild %s (%d/%d)", guild_id, total_count, MAX_TOTAL_QUESTIONS)
            return None

        added_by_name = (getattr(added_by, "display_name", None) or getattr(added_by, "name", None) or str(added_by)) if added_by else None
        if added_by_name is not None and not isinstance(added_by_name, str):
            added_by_name = str(added_by_name)
        cursor = await self.db.execute(
            """
            INSERT INTO questions (
                guild_id, channel_id, question, status, source, created_at,
                added_by_id, added_by_name, suggested_by_id, suggested_by_name
            ) VALUES (?, ?, ?, 'to_ask', ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                question_clean,
                source,
                datetime.now(timezone.utc).isoformat(),
                added_by.id if added_by else None,
                added_by_name,
                suggested_by_id,
                suggested_by_name,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

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
        cur_queue = await self.get_queue_count(guild_id, channel_id=channel_id)
        if cur_queue >= MAX_QUEUE_QUESTIONS:
            return False
        result = await self.db.execute(
            "UPDATE questions SET status = 'to_ask', asked_at = NULL WHERE guild_id = ? AND id = ?",
            (guild_id, question_id),
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
            "INSERT INTO questions (guild_id, channel_id, question, status, source, created_at) VALUES (?, ?, ?, 'to_ask', 'default', ?)",
            [(guild_id, channel_id, q, now) for q in questions_list],
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
            INSERT INTO suggestions (id, guild_id, target_channel_id, question, message, avatar_url, user_id, user_name, created_at, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (suggestion_id, guild_id, target_channel_id, q, note_clean, avatar_url, user.id, user_name_str, now_str, stored_locale),
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
            except Exception:
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
        target_ch_id = suggestion.get("target_channel_id")
        if target_ch_id:
            embed.add_field(name=t(lang, "sugg_target_channel"), value=f"<#{target_ch_id}>", inline=True)
        if suggestion.get("message"):
            embed.add_field(name=t(lang, "sugg_review_note"), value=suggestion["message"], inline=False)
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
    ) -> Tuple[bool, str]:
        cursor = await self.db.execute("SELECT * FROM suggestions WHERE id = ? AND guild_id = ?", (suggestion_id, guild_id))
        pre_row = await cursor.fetchone()
        if not pre_row:
            return False, "not_found"

        target_channel_id = pre_row["target_channel_id"]
        queue_count = await self.get_queue_count(guild_id, channel_id=target_channel_id)
        if queue_count >= MAX_QUEUE_QUESTIONS:
            return False, "queue_full"

        total_count = await self.get_total_question_count(guild_id)
        if total_count >= MAX_TOTAL_QUESTIONS:
            return False, "total_limit"

        # Atomically claim the suggestion to prevent race conditions
        cursor = await self.db.execute(
            "DELETE FROM suggestions WHERE id = ? AND guild_id = ? RETURNING *",
            (suggestion_id, guild_id),
        )
        row = await cursor.fetchone()
        if not row:
            return False, "not_found"
        suggestion = dict(row)
        await self.db.commit()

        chosen_question = (question or suggestion["question"]).strip()[:MAX_QUESTION_LENGTH]
        await self.add_question(
            guild_id,
            chosen_question,
            source="suggestion",
            added_by=added_by,
            suggested_by_id=suggestion["user_id"],
            suggested_by_name=suggestion["user_name"],
            channel_id=target_channel_id,
        )

        server_lang = self.get_server_language(guild_id)

        # Update the admin channel message
        msg_to_update = review_message
        if not msg_to_update and suggestion.get("review_message_id") and suggestion.get("admin_channel_id"):
            ch = self.bot.get_channel(suggestion["admin_channel_id"])
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(suggestion["admin_channel_id"])
                except Exception:
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
            except Exception:
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

        # Notify suggester via DM (in suggester's language)
        try:
            user = self.bot.get_user(suggestion["user_id"]) or await self.bot.fetch_user(suggestion["user_id"])
            guild = self.bot.get_guild(guild_id)
            user_dm_lang = suggestion.get("language") or server_lang

            ch_obj = self.bot.get_channel(target_channel_id) if target_channel_id else None
            ch_name = f"#{ch_obj.name}" if ch_obj else ""

            if ch_name:
                dm_desc = t(user_dm_lang, "sugg_dm_approved_desc_chan", question=chosen_question, channel=ch_name)
            else:
                dm_desc = t(user_dm_lang, "sugg_dm_approved_desc", question=chosen_question)

            dm_embed = discord.Embed(
                title=t(user_dm_lang, "sugg_dm_approved_title"),
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
                except Exception:
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
            except Exception:
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

    async def build_questions_embed(
        self, guild_id: int, status: str, page: int = 0, channel_id: Optional[int] = None, lang: Optional[str] = None
    ) -> discord.Embed:
        lang = lang or self.get_server_language(guild_id)
        page_size = 10
        if channel_id:
            cursor = await self.db.execute("SELECT COUNT(*) FROM questions WHERE guild_id = ? AND channel_id = ? AND status = ?", (guild_id, channel_id, status))
        else:
            cursor = await self.db.execute("SELECT COUNT(*) FROM questions WHERE guild_id = ? AND status = ?", (guild_id, status))
        total_count = (await cursor.fetchone())[0]
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        rows = await self.get_questions_page(guild_id, status, page, page_size, channel_id=channel_id)

        is_queue = (status == "to_ask")
        base_title = t(lang, "queue_title") if is_queue else t(lang, "history_title")
        if channel_id:
            ch_obj = self.bot.get_channel(channel_id)
            ch_name = f"#{ch_obj.name}" if ch_obj else f"#{channel_id}"
            title = f"{base_title} • {ch_name}"
        else:
            title = base_title
        colour = COLOR_QUEUE if is_queue else COLOR_HISTORY

        embed = discord.Embed(title=title, colour=colour)
        if not rows:
            embed.description = t(lang, "queue_empty") if is_queue else t(lang, "history_empty")
            embed.set_footer(text=t(lang, "page_single"))
            return embed

        lines = []
        for idx, r in enumerate(rows, 1):
            item_num = page * page_size + idx
            lines.append(f"`{item_num}.` {r['question']}")

        embed.description = "\n".join(lines)
        embed.set_footer(text=t(lang, "page_footer", page=page + 1, total_pages=total_pages, shown=len(rows), total=total_count))
        return embed

    async def build_suggestions_embed(self, guild_id: int, page: int = 0, lang: Optional[str] = None) -> discord.Embed:
        lang = lang or self.get_server_language(guild_id)
        page_size = 10
        cursor = await self.db.execute("SELECT COUNT(*) FROM suggestions WHERE guild_id = ?", (guild_id,))
        total_count = (await cursor.fetchone())[0]
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        rows = await self.get_suggestions_page(guild_id, page, page_size)

        embed = discord.Embed(
            title=t(lang, "sugg_list_title", total=total_count),
            colour=COLOR_SUGGESTIONS,
        )
        if not rows:
            embed.description = t(lang, "sugg_list_empty")
            embed.set_footer(text=t(lang, "page_single"))
            return embed

        for idx, r in enumerate(rows, 1):
            item_num = page * page_size + idx
            date_str = r["created_at"][:10]
            target_str = f" • <#{r['target_channel_id']}>" if r.get("target_channel_id") else ""
            user_part = t(lang, "sugg_from_user", user=r['user_name'], date=date_str) + target_str
            q_part = r["question"]
            note_part = f"\n*{r['message']}*" if r.get("message") else ""
            embed.add_field(
                name=f"{item_num}. {user_part}",
                value=f"{q_part}{note_part}",
                inline=False,
            )
        embed.set_footer(text=t(lang, "page_footer", page=page + 1, total_pages=total_pages, shown=len(rows), total=total_count))
        return embed

    async def build_question_detail_embed(self, guild_id: int, row: Dict[str, Any], lang: Optional[str] = None) -> discord.Embed:
        lang = lang or self.get_server_language(guild_id)
        is_queue = (row["status"] == "to_ask")
        status_text = t(lang, "q_detail_status_queue") if is_queue else t(lang, "q_detail_status_asked", date=format_discord_timestamp(row.get('asked_at'), 'f', lang=lang))
        colour = COLOR_QUEUE if is_queue else COLOR_HISTORY

        embed = discord.Embed(
            title=t(lang, "q_detail_title", id=row['id']),
            description=f"### {row['question']}",
            colour=colour,
        )
        embed.add_field(name=t(lang, "status_label"), value=status_text, inline=True)

        chan_id = row.get("channel_id")
        if chan_id:
            embed.add_field(name=t(lang, "channel_label"), value=f"<#{chan_id}>", inline=True)

        if row.get("suggested_by_name"):
            source_val = f"💡 {row['suggested_by_name']}"
        elif row.get("added_by_name"):
            source_val = f"👤 {row['added_by_name']}"
        else:
            source_val = t(lang, "default_system_question")
        embed.add_field(name=t(lang, "source_author_label"), value=source_val, inline=True)

        embed.add_field(
            name=t(lang, "created_label"),
            value=format_discord_timestamp(row.get("created_at"), "R", lang=lang),
            inline=True,
        )
        if not is_queue and row.get("asked_at"):
            embed.add_field(
                name=t(lang, "posted_label"),
                value=format_discord_timestamp(row.get("asked_at"), "R", lang=lang),
                inline=True,
            )
        embed.set_footer(text=t(lang, "q_detail_footer"))
        return embed

    async def build_suggestion_detail_embed(self, guild_id: int, row: Dict[str, Any], lang: Optional[str] = None) -> discord.Embed:
        lang = lang or self.get_server_language(guild_id)
        embed = discord.Embed(
            title=t(lang, "sugg_detail_title"),
            description=f"### {row['question']}",
            colour=COLOR_SUGGESTIONS,
        )
        embed.set_author(
            name=row["user_name"],
            icon_url=row["avatar_url"] if row.get("avatar_url") else None,
        )
        target_ch_id = row.get("target_channel_id")
        if target_ch_id:
            embed.add_field(name=t(lang, "sugg_target_channel"), value=f"<#{target_ch_id}>", inline=True)
        if row.get("message"):
            embed.add_field(name=t(lang, "sugg_detail_note_label"), value=row["message"], inline=False)
        embed.add_field(
            name=t(lang, "created_label"),
            value=format_discord_timestamp(row.get("created_at"), "R", lang=lang),
            inline=True,
        )
        embed.set_footer(text=t(lang, "sugg_detail_footer"))
        return embed

    async def build_settings_embed(self, guild_id: int, lang: Optional[str] = None) -> discord.Embed:
        effective_lang = lang or self.get_user_language(None, guild_id)
        server_lang = self.get_server_language(guild_id)
        guild_settings = await self.get_guild_settings(guild_id)
        channels = await self.get_qotd_channels(guild_id)

        cursor = await self.db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM questions WHERE guild_id = ? AND status = 'to_ask') AS to_ask_cnt,
                (SELECT COUNT(*) FROM questions WHERE guild_id = ? AND status = 'asked') AS asked_cnt,
                (SELECT COUNT(*) FROM suggestions WHERE guild_id = ?) AS sugg_cnt
            """,
            (guild_id, guild_id, guild_id),
        )
        counts = await cursor.fetchone()
        c1, c2, c3 = counts[0], counts[1], counts[2]

        embed = discord.Embed(title=t(effective_lang, "settings_title"), colour=COLOR_SETTINGS)
        adm_id = guild_settings.get("admin_channel_id")
        embed.add_field(
            name=t(effective_lang, "settings_admin_channel"),
            value=f"<#{adm_id}>" if adm_id else t(effective_lang, "settings_not_set"),
            inline=True,
        )

        server_lang_name = SUPPORTED_LANGUAGES.get(server_lang, server_lang)
        embed.add_field(name=t(effective_lang, "settings_language"), value=f"**{server_lang_name}**", inline=True)

        if channels:
            chan_lines = []
            for idx, c in enumerate(channels, 1):
                cid = c["channel_id"]
                sched = c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME
                role_str = f"<@&{c['role_id']}>" if c.get("role_id") else t(effective_lang, "settings_not_used")
                q_cnt = await self.get_queue_count(guild_id, channel_id=cid)
                chan_lines.append(f"`{idx}.` <#{cid}> • ⏰ **{sched}** | 🔔 {role_str} | 📥 Queue: **{q_cnt}**")
            embed.add_field(
                name=t(effective_lang, "settings_active_channels"),
                value="\n".join(chan_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name=t(effective_lang, "settings_active_channels"),
                value=t(effective_lang, "settings_no_channels"),
                inline=False,
            )

        embed.add_field(
            name=t(effective_lang, "settings_db_status"),
            value=t(effective_lang, "settings_db_status_val", queue=c1, max_queue=MAX_QUEUE_QUESTIONS, history=c2, total=c1 + c2, max_total=MAX_TOTAL_QUESTIONS, sugg=c3, max_sugg=MAX_PENDING_SUGGESTIONS),
            inline=False,
        )
        embed.set_footer(text=t(effective_lang, "settings_footer"))
        return embed

    async def build_channel_manage_embed(self, guild_id: int, channel_id: int, lang: Optional[str] = None) -> discord.Embed:
        effective_lang = lang or self.get_user_language(None, guild_id)
        c = await self.get_qotd_channel(channel_id)
        discord_ch = self.bot.get_channel(channel_id)
        ch_name = discord_ch.name if discord_ch else str(channel_id)

        embed = discord.Embed(
            title=t(effective_lang, "channel_settings_title", channel=ch_name),
            colour=COLOR_SETTINGS,
        )
        if not c:
            embed.description = "⚠️ Channel configuration not found."
            return embed

        embed.add_field(name="📢 " + t(effective_lang, "channel_label"), value=f"<#{channel_id}>", inline=True)
        sched = c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME
        embed.add_field(name="⏰ " + t(effective_lang, "btn_manage_channel_hour"), value=f"**{sched}** (Europe/Prague)", inline=True)
        role_str = f"<@&{c['role_id']}>" if c.get("role_id") else t(effective_lang, "settings_not_used")
        embed.add_field(name="🔔 " + t(effective_lang, "btn_manage_channel_role"), value=role_str, inline=True)

        thresh = c.get("low_queue_threshold") or 3
        embed.add_field(name="⚠️ " + t(effective_lang, "btn_manage_channel_threshold"), value=f"**{thresh}** questions", inline=True)

        q_count = await self.get_queue_count(guild_id, channel_id=channel_id)
        h_count = await self.get_total_question_count(guild_id, channel_id=channel_id) - q_count
        embed.add_field(name="📊 Queue / History", value=f"Queue: **{q_count}** | History: **{h_count}**", inline=True)

        last_date = c.get("last_posted_date") or "None"
        embed.add_field(name="📅 Last Posted Date", value=f"`{last_date}`", inline=True)
        return embed

    async def build_top_embed(self, guild_id: int, lang: Optional[str] = None) -> discord.Embed:
        lang = lang or self.get_server_language(guild_id)
        cursor = await self.db.execute(
            """
            SELECT suggested_by_id, suggested_by_name, COUNT(*) AS accepted_count
            FROM questions
            WHERE guild_id = ? AND source = 'suggestion' AND suggested_by_id IS NOT NULL
            GROUP BY suggested_by_id
            ORDER BY accepted_count DESC
            LIMIT 10
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        embed = discord.Embed(title=t(lang, "top_title"), colour=COLOR_LEADERBOARD)
        if not rows:
            embed.description = t(lang, "top_empty")
            embed.set_footer(text=t(lang, "page_single"))
            return embed

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for rank, r in enumerate(rows, 1):
            badge = medals[rank - 1] if rank <= 3 else f"`{rank}.`"
            approved_label = t(lang, "approved_questions_count")
            lines.append(f"{badge} **{r['suggested_by_name']}** — {r['accepted_count']} {approved_label}")

        embed.description = "\n".join(lines)
        embed.set_footer(text=t(lang, "top_footer"))
        return embed

    async def build_info_embed(self, guild_id: int, lang: Optional[str] = None) -> discord.Embed:
        effective_lang = lang or self.get_user_language(None, guild_id)
        channels = await self.get_qotd_channels(guild_id)

        sched_time = DEFAULT_SCHEDULED_TIME
        if len(channels) == 1:
            sched_time = channels[0].get("scheduled_time") or DEFAULT_SCHEDULED_TIME

        embed = discord.Embed(
            title=t(effective_lang, "info_title"),
            description=t(effective_lang, "info_desc", time=sched_time),
            colour=COLOR_DETAIL,
        )
        embed.add_field(
            name=t(effective_lang, "info_f1_name"),
            value=t(effective_lang, "info_f1_val"),
            inline=False,
        )
        embed.add_field(
            name=t(effective_lang, "info_f2_name"),
            value=t(effective_lang, "info_f2_val"),
            inline=False,
        )
        embed.add_field(
            name=t(effective_lang, "info_f3_name"),
            value=t(effective_lang, "info_f3_val"),
            inline=False,
        )

        # If there are multiple active QOTD channels, show a clean summary field of channel schedules
        if len(channels) > 1:
            guild = self.bot.get_guild(guild_id)
            ch_lines = []
            for c in channels[:10]:
                ch_obj = self.bot.get_channel(c["channel_id"]) or (guild.get_channel(c["channel_id"]) if guild else None)
                ch_name = f"<#{c['channel_id']}>" if ch_obj else f"Channel {c['channel_id']}"
                sched = c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME
                ch_lines.append(f"• {ch_name} — **{sched}**")
            embed.add_field(
                name="📅 " + t(effective_lang, "available_qotd_channels_label"),
                value="\n".join(ch_lines),
                inline=False,
            )

        embed.set_footer(text=t(effective_lang, "info_footer"))
        return embed

    # -----------------------------------------------------------------------
    # Navigation Handler
    # -----------------------------------------------------------------------

    async def show_menu_section(
        self, interaction: discord.Interaction, section: str, channel_id: Optional[int] = None, lang: Optional[str] = None
    ):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        effective_lang = lang or self.get_user_language(interaction, guild_id)
        channels = await self.get_qotd_channels(guild_id)

        # Resolve active channel_id for queue/history
        active_channel_id = channel_id
        if channels and (active_channel_id is None or active_channel_id not in [c["channel_id"] for c in channels]):
            if interaction.channel_id in [c["channel_id"] for c in channels]:
                active_channel_id = interaction.channel_id
            else:
                active_channel_id = channels[0]["channel_id"]

        if section == "to_ask":
            embed = await self.build_questions_embed(guild_id, "to_ask", 0, channel_id=active_channel_id, lang=effective_lang)
            total_pages = await self.question_page_count(guild_id, "to_ask", channel_id=active_channel_id)
            page_items = await self.get_questions_page(guild_id, "to_ask", 0, channel_id=active_channel_id)
            view = QotdPanelView(
                self,
                section="to_ask",
                page=0,
                guild_id=guild_id,
                total_pages=total_pages,
                page_items=page_items,
                channel_id=active_channel_id,
                channels=channels,
                lang=effective_lang,
            )
        elif section == "asked":
            embed = await self.build_questions_embed(guild_id, "asked", 0, channel_id=active_channel_id, lang=effective_lang)
            total_pages = await self.question_page_count(guild_id, "asked", channel_id=active_channel_id)
            page_items = await self.get_questions_page(guild_id, "asked", 0, channel_id=active_channel_id)
            view = QotdPanelView(
                self,
                section="asked",
                page=0,
                guild_id=guild_id,
                total_pages=total_pages,
                page_items=page_items,
                channel_id=active_channel_id,
                channels=channels,
                lang=effective_lang,
            )
        elif section == "suggestions":
            embed = await self.build_suggestions_embed(guild_id, 0, lang=effective_lang)
            total_pages = await self.suggestions_page_count(guild_id)
            page_items = await self.get_suggestions_page(guild_id, 0)
            view = QotdPanelView(self, section="suggestions", page=0, guild_id=guild_id, total_pages=total_pages, page_items=page_items, lang=effective_lang)
        elif section == "settings":
            embed = await self.build_settings_embed(guild_id, lang=effective_lang)
            view = QotdPanelView(self, section="settings", guild_id=guild_id, channels=channels, lang=effective_lang)
        elif section == "top":
            embed = await self.build_top_embed(guild_id, lang=effective_lang)
            view = QotdPanelView(self, section="top", guild_id=guild_id, lang=effective_lang)
        else:
            embed = await self.build_info_embed(guild_id, lang=effective_lang)
            view = QotdPanelView(self, section="info", guild_id=guild_id, lang=effective_lang)

        msg = await interaction.edit_original_response(embed=embed, view=view)
        if isinstance(view, BaseTimeoutView):
            view.set_message(msg)

    # -----------------------------------------------------------------------
    # Publishing Logic & Scheduler (Per-Channel)
    # -----------------------------------------------------------------------

    async def warn_low_queue(self, guild_id: int, channel_id: int):
        c = await self.get_qotd_channel(channel_id)
        if not c:
            return
        threshold = c.get("low_queue_threshold") or 3
        cursor = await self.db.execute("SELECT COUNT(*) FROM questions WHERE guild_id = ? AND channel_id = ? AND status = 'to_ask'", (guild_id, channel_id))
        remaining = (await cursor.fetchone())[0]
        if remaining > threshold:
            return

        settings = await self.get_guild_settings(guild_id)
        admin_channel_id = settings.get("admin_channel_id", 0)
        if not admin_channel_id:
            return

        admin_channel = self.bot.get_channel(admin_channel_id)
        if admin_channel is None:
            try:
                admin_channel = await self.bot.fetch_channel(admin_channel_id)
            except Exception:
                return

        if not isinstance(admin_channel, (discord.TextChannel, discord.Thread)):
            return

        lang = self.get_server_language(guild_id)
        if remaining == 0:
            title = t(lang, "warning_queue_empty_title")
            desc = t(lang, "warning_queue_empty_channel_desc", channel_id=channel_id)
            colour = COLOR_DANGER
        else:
            title = t(lang, "warning_queue_low_title")
            desc = t(lang, "warning_queue_low_channel_desc", channel_id=channel_id, count=remaining)
            colour = COLOR_SUGGESTIONS

        embed = discord.Embed(title=title, description=desc, colour=colour)
        try:
            await admin_channel.send(embed=embed)
        except Exception as error:
            logger.warning("Could not send low queue warning in guild %s for channel %s: %s", guild_id, channel_id, error)

    async def post_question(
        self,
        guild_id: int,
        channel_id: int,
        question_id: Optional[int] = None,
        manual_question: Optional[str] = None,
    ) -> bool:
        # Per-channel lock prevents duplicate dispatch when scheduler and
        # manual triggers (/qotd_send, "Send now") race concurrently.
        async with self._get_channel_lock(channel_id):
            c = await self.get_qotd_channel(channel_id)
            if not c:
                return False

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    return False

            guild = self.bot.get_guild(guild_id)
            role = guild.get_role(c["role_id"]) if guild and c.get("role_id") else None

            if not isinstance(channel, discord.TextChannel):
                logger.warning("Invalid QOTD channel %s for guild %s", channel_id, guild_id)
                return False

            queued_row = None
            if question_id:
                queued_row = await self.get_question(guild_id, question_id)
                if not queued_row:
                    return False
                final_question = queued_row["question"]
            elif manual_question:
                final_question = manual_question.strip()[:MAX_QUESTION_LENGTH]
                qid = await self.add_question(guild_id, final_question, source="manual", bypass_queue_limit=True, channel_id=channel_id)
                if not qid:
                    return False
                queued_row = await self.get_question(guild_id, qid)
            else:
                queued_row = await self.get_next_question(guild_id, channel_id=channel_id)
                if not queued_row:
                    return False
                final_question = queued_row["question"]

            lang = self.get_server_language(guild_id)

            # Attribution
            suggested_name = queued_row.get("suggested_by_name") if queued_row else None
            added_name = queued_row.get("added_by_name") if queued_row else None
            if suggested_name:
                attribution = t(lang, "attribution_suggested", name=suggested_name)
            elif added_name:
                attribution = t(lang, "attribution_added", name=added_name)
            else:
                attribution = t(lang, "attribution_default")

            attr_user_id = (queued_row.get("suggested_by_id") or queued_row.get("added_by_id")) if queued_row else None
            attr_user: Optional[discord.User] = None
            if attr_user_id:
                attr_user = self.bot.get_user(attr_user_id)
                if not attr_user:
                    try:
                        attr_user = await self.bot.fetch_user(attr_user_id)
                    except Exception:
                        pass

            question_number = (c.get("qotd_number") or 0) + 1
            embed = discord.Embed(
                description=f"### {final_question}",
                colour=COLOR_POST,
                timestamp=datetime.now(timezone.utc),
            )
            footer_text = t(lang, "question_footer", attribution=attribution, number=question_number)
            if attr_user and getattr(attr_user, "display_avatar", None):
                embed.set_footer(text=footer_text, icon_url=attr_user.display_avatar.url)
            else:
                embed.set_footer(text=footer_text)

            try:
                # Archive previous thread if any
                last_thread_id = c.get("last_thread_id")
                if last_thread_id:
                    try:
                        th = self.bot.get_channel(last_thread_id) or await self.bot.fetch_channel(last_thread_id)
                        if isinstance(th, discord.Thread) and not th.archived:
                            await th.edit(archived=True, locked=True)
                    except Exception as th_err:
                        logger.debug("Could not archive thread %s: %s", last_thread_id, th_err)

                content = role.mention if role else None
                allowed = discord.AllowedMentions(roles=True) if role else discord.AllowedMentions.none()
                msg = await channel.send(
                    content=content,
                    embed=embed,
                    view=QotdView(self, lang=lang),
                    allowed_mentions=allowed,
                )

                new_thread_id = None
                try:
                    thread = await msg.create_thread(name=t(lang, "thread_title", number=question_number))
                    new_thread_id = thread.id
                except (discord.Forbidden, discord.HTTPException) as th_err:
                    logger.warning("Could not create thread for QOTD in channel %s: %s", channel_id, th_err)

                if queued_row is not None:
                    await self.mark_question_asked(queued_row["id"])

                today = datetime.now(QOTD_TIMEZONE).date().isoformat()
                await self.update_qotd_channel(
                    channel_id,
                    qotd_number=question_number,
                    last_posted_date=today,
                    last_thread_id=new_thread_id,
                )

                await self.warn_low_queue(guild_id, channel_id)
                return True
            except discord.Forbidden as f_err:
                logger.error("Missing permissions to post QOTD in channel %s: %s", channel_id, f_err)
                return False
            except Exception as err:
                logger.error("Failed to post QOTD in channel %s: %s", channel_id, err)
                return False

    @tasks.loop(minutes=1)
    async def qotd_scheduler(self):
        now = datetime.now(QOTD_TIMEZONE)
        today = now.date().isoformat()

        try:
            cursor = await self.db.execute("SELECT * FROM qotd_channels")
            channels = await cursor.fetchall()
        except Exception as err:
            logger.error("Failed to query qotd_channels in qotd_scheduler: %s", err)
            return

        for ch in channels:
            chan_id = ch["channel_id"]
            guild_id = ch["guild_id"]
            last_date = ch["last_posted_date"]

            if last_date == today:
                continue

            scheduled_raw = (ch["scheduled_time"] or DEFAULT_SCHEDULED_TIME).strip()
            is_due = False
            if ":" in scheduled_raw:
                try:
                    s_time = datetime.strptime(scheduled_raw, "%H:%M").time()
                    if s_time <= now.time():
                        is_due = True
                except Exception:
                    is_due = (now.hour >= 9)
            else:
                try:
                    s_hour = int(scheduled_raw)
                    if now.hour >= s_hour:
                        is_due = True
                except Exception:
                    is_due = (now.hour >= 9)

            if not is_due:
                continue

            try:
                next_q = await self.get_next_question(guild_id, channel_id=chan_id)
                if not next_q:
                    logger.warning("No questions in queue for channel %s (guild %s) at scheduled time %s", chan_id, guild_id, scheduled_raw)
                    await self.warn_low_queue(guild_id, chan_id)
                    # Mark last_posted_date as today so we don't spam warnings every minute
                    await self.update_qotd_channel(chan_id, last_posted_date=today)
                else:
                    logger.info("Dispatching scheduled QOTD for channel %s (guild %s, scheduled %s, current %s)", chan_id, guild_id, scheduled_raw, now.strftime('%H:%M:%S'))
                    success = await self.post_question(guild_id, chan_id, question_id=next_q["id"])
                    if success:
                        logger.info("Successfully posted scheduled QOTD in channel %s", chan_id)
                    else:
                        logger.error("post_question returned False for channel %s", chan_id)

                # Respect rate limits between channel dispatches
                await asyncio.sleep(0.5)
            except Exception as chan_err:
                logger.error("Error dispatching QOTD for channel %s in guild %s: %s", chan_id, guild_id, chan_err)

    @qotd_scheduler.before_loop
    async def before_qotd_scheduler(self):
        await self.bot.wait_until_ready()

    @qotd_scheduler.error
    async def qotd_scheduler_error(self, error):
        logger.error("Unhandled error in qotd_scheduler loop: %s", error)

    # -----------------------------------------------------------------------
    # Cog Listeners
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if not channel.guild:
            return
        row = await self.get_qotd_channel(channel.id)
        if row:
            logger.info("QOTD channel #%s (%s) in guild %s was deleted from Discord. Unlinking...", channel.name, channel.id, channel.guild.id)
            await self.delete_qotd_channel(channel.id, delete_questions=False)

    # -----------------------------------------------------------------------
    # Slash Commands
    # -----------------------------------------------------------------------

    @app_commands.command(name="qotd_language", description="Change bot language / Změnit jazyk bota")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(language="Select bot language / Vyberte jazyk bota")
    @app_commands.choices(
        language=[
            app_commands.Choice(name="English 🇬🇧", value="en"),
            app_commands.Choice(name="Čeština 🇨🇿", value="cs"),
            app_commands.Choice(name="Español 🇪🇸", value="es"),
            app_commands.Choice(name="Português 🇵🇹", value="pt"),
            app_commands.Choice(name="Slovenčina 🇸🇰", value="sk"),
            app_commands.Choice(name="Deutsch 🇩🇪", value="de"),
            app_commands.Choice(name="Français 🇫🇷", value="fr"),
        ]
    )
    async def qotd_language(self, interaction: discord.Interaction, language: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        new_lang = language.value
        await self.set_guild_language(interaction.guild_id, new_lang)
        lang_name = SUPPORTED_LANGUAGES.get(new_lang, new_lang)
        user_lang = self.get_user_language(interaction, interaction.guild_id)
        await interaction.followup.send(
            t(user_lang, "lang_changed", language=lang_name),
            ephemeral=True,
        )

    @app_commands.command(name="qotd", description="Open interactive QOTD control panel / Otevřít ovládací panel QOTD")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        section="Jump directly to a specific panel section / Přejít do sekce",
        channel="Target QOTD channel for queue/history / Cílový QOTD kanál",
    )
    @app_commands.choices(
        section=[
            app_commands.Choice(name="📌 Questions in Queue / Fronta", value="to_ask"),
            app_commands.Choice(name="💡 Suggestions / Návrhy", value="suggestions"),
            app_commands.Choice(name="📜 Question History / Historie", value="asked"),
            app_commands.Choice(name="⚙️ Settings & Stats / Nastavení", value="settings"),
            app_commands.Choice(name="🏆 Leaderboard / Žebříček", value="top"),
        ]
    )
    async def qotd(
        self,
        interaction: discord.Interaction,
        section: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        sec = section.value if section else "to_ask"
        user_lang = self.get_user_language(interaction, interaction.guild_id)
        await self.show_menu_section(interaction, sec, channel_id=channel.id if channel else None, lang=user_lang)

    @app_commands.command(name="qotd_add", description="Add a new question to the queue / Přidat novou otázku do fronty")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        question="Question text / Znění otázky",
        channel="Target QOTD channel (optional) / Cílový QOTD kanál",
    )
    async def qotd_add(self, interaction: discord.Interaction, question: str, channel: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        user_lang = self.get_user_language(interaction, interaction.guild_id)
        question = question.strip()
        if not question:
            await interaction.followup.send(t(user_lang, "limit_empty_question"), ephemeral=True)
            return

        if len(question) > MAX_QUESTION_LENGTH:
            await interaction.followup.send(
                t(user_lang, "limit_question_too_long", max_len=MAX_QUESTION_LENGTH, len=len(question)),
                ephemeral=True,
            )
            return

        channels = await self.get_qotd_channels(interaction.guild_id)
        if not channels:
            await interaction.followup.send(t(user_lang, "no_channels_configured"), ephemeral=True)
            return

        target_ch_id = None
        if channel:
            qotd_ch = await self.find_qotd_channel(interaction.guild_id, channel)
            if not qotd_ch:
                await interaction.followup.send(t(user_lang, "channel_not_qotd_generic"), ephemeral=True)
                return
            target_ch_id = qotd_ch["channel_id"]
        else:
            if len(channels) == 1:
                target_ch_id = channels[0]["channel_id"]
            elif interaction.channel_id in [c["channel_id"] for c in channels]:
                target_ch_id = interaction.channel_id
            else:
                target_ch_id = channels[0]["channel_id"]

        queue_count = await self.get_queue_count(interaction.guild_id, channel_id=target_ch_id)
        if queue_count >= MAX_QUEUE_QUESTIONS:
            await interaction.followup.send(
                t(user_lang, "limit_queue_full", max_q=MAX_QUEUE_QUESTIONS),
                ephemeral=True,
            )
            return

        total_count = await self.get_total_question_count(interaction.guild_id)
        if total_count >= MAX_TOTAL_QUESTIONS:
            await interaction.followup.send(
                t(user_lang, "limit_total_full", max_total=MAX_TOTAL_QUESTIONS),
                ephemeral=True,
            )
            return

        unique, duplicates = await self.check_duplicates(interaction.guild_id, [question], channel_id=target_ch_id)
        if duplicates:
            await interaction.followup.send(t(user_lang, "duplicate_detected"), ephemeral=True)
            return

        qid = await self.add_question(
            interaction.guild_id,
            question,
            source="manual",
            added_by=interaction.user,
            channel_id=target_ch_id,
        )
        if not qid:
            await interaction.followup.send(t(user_lang, "qotd_add_storage_error"), ephemeral=True)
            return

        await interaction.followup.send(t(user_lang, "qotd_add_success", qid=qid, question=question), ephemeral=True)

    @qotd_add.autocomplete("channel")
    async def qotd_add_channel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        if not interaction.guild_id:
            return []
        channels = await self.get_qotd_channels(interaction.guild_id)
        choices = []
        for c in channels:
            ch_id = c["channel_id"]
            ch = interaction.guild.get_channel(ch_id) if interaction.guild else None
            name = f"#{ch.name}" if ch else f"Channel {ch_id}"
            if not current or current.lower() in name.lower() or current.lower() in str(ch_id):
                choices.append(app_commands.Choice(name=name[:100], value=str(ch_id)))
        return choices[:25]

    @app_commands.command(name="qotd_suggest", description="Suggest a question for QOTD / Navrhnout otázku pro QOTD")
    @app_commands.guild_only()
    @app_commands.describe(
        channel="Target QOTD channel (optional) / Cílový QOTD kanál",
        question="Your question (optional) / Vaše otázka",
        note="Optional note for administrators (optional) / Volitelná poznámka",
    )
    async def qotd_suggest(
        self,
        interaction: discord.Interaction,
        channel: Optional[str] = None,
        question: Optional[str] = None,
        note: Optional[str] = None,
    ):
        user_lang = self.get_user_language(interaction, interaction.guild_id)
        channels = await self.get_qotd_channels(interaction.guild_id)
        if not channels:
            await interaction.response.send_message(t(user_lang, "no_channels_configured"), ephemeral=True)
            return

        target_ch_id: Optional[int] = None
        if channel:
            qotd_ch = await self.find_qotd_channel(interaction.guild_id, channel)
            if not qotd_ch:
                await interaction.response.send_message(t(user_lang, "channel_not_qotd_generic"), ephemeral=True)
                return
            target_ch_id = qotd_ch["channel_id"]
        elif interaction.channel_id in [c["channel_id"] for c in channels]:
            target_ch_id = interaction.channel_id

        # The channel selection is done in the form
        modal = SuggestionModal(
            self,
            interaction.guild_id,
            channels=channels,
            target_channel_id=target_ch_id,
            prefilled_question=question,
            prefilled_note=note,
            lang=user_lang,
        )
        await interaction.response.send_modal(modal)

    @qotd_suggest.autocomplete("channel")
    async def qotd_suggest_channel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        if not interaction.guild_id:
            return []
        channels = await self.get_qotd_channels(interaction.guild_id)
        choices = []
        for c in channels:
            ch_id = c["channel_id"]
            ch = interaction.guild.get_channel(ch_id) if interaction.guild else None
            name = f"#{ch.name}" if ch else f"Channel {ch_id}"
            if not current or current.lower() in name.lower() or current.lower() in str(ch_id):
                choices.append(app_commands.Choice(name=name[:100], value=str(ch_id)))
        return choices[:25]

    @app_commands.command(name="qotd_send", description="Send next QOTD from queue immediately / Ihned odeslat další otázku")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Target QOTD channel (optional if only 1 channel exists) / Cílový QOTD kanál",
    )
    async def qotd_send(self, interaction: discord.Interaction, channel: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        user_lang = self.get_user_language(interaction, interaction.guild_id)
        channels = await self.get_qotd_channels(interaction.guild_id)
        if not channels:
            await interaction.followup.send(t(user_lang, "no_channels_configured"), ephemeral=True)
            return

        target_ch_id = None
        if channel:
            qotd_ch = await self.find_qotd_channel(interaction.guild_id, channel)
            if not qotd_ch:
                await interaction.followup.send(t(user_lang, "channel_not_qotd_generic"), ephemeral=True)
                return
            target_ch_id = qotd_ch["channel_id"]
        else:
            if len(channels) == 1:
                target_ch_id = channels[0]["channel_id"]
            elif interaction.channel_id in [c["channel_id"] for c in channels]:
                target_ch_id = interaction.channel_id
            else:
                target_ch_id = channels[0]["channel_id"]

        if await self.post_question(interaction.guild_id, target_ch_id):
            await interaction.followup.send(t(user_lang, "qotd_send_success"), ephemeral=True)
        else:
            await interaction.followup.send(t(user_lang, "qotd_send_error"), ephemeral=True)

    @qotd_send.autocomplete("channel")
    async def qotd_send_channel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        if not interaction.guild_id:
            return []
        channels = await self.get_qotd_channels(interaction.guild_id)
        choices = []
        for c in channels:
            ch_id = c["channel_id"]
            ch = interaction.guild.get_channel(ch_id) if interaction.guild else None
            name = f"#{ch.name}" if ch else f"Channel {ch_id}"
            if not current or current.lower() in name.lower() or current.lower() in str(ch_id):
                choices.append(app_commands.Choice(name=name[:100], value=str(ch_id)))
        return choices[:25]

    @app_commands.command(name="qotd_top", description="View top question contributors / Zobrazit žebříček přispěvatelů")
    @app_commands.guild_only()
    async def qotd_top(self, interaction: discord.Interaction):
        await interaction.response.defer()
        server_lang = self.get_server_language(interaction.guild_id)
        embed = await self.build_top_embed(interaction.guild_id, lang=server_lang)
        await interaction.followup.send(embed=embed, ephemeral=False)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            user_lang = self.get_user_language(interaction, interaction.guild_id)
            msg = t(user_lang, "admin_only")
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            logger.error("Unhandled app command error in Qotd cog: %s", error, exc_info=error)


async def setup(bot: commands.Bot):
    await bot.add_cog(Qotd(bot))
