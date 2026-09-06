import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from translations import t, DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, resolve_user_locale
from .constants import (
    logger,
    DATABASE_FILE,
    DEFAULT_SCHEDULED_TIME,
    MAX_QUEUE_QUESTIONS,
    MAX_TOTAL_QUESTIONS,
    MAX_QUESTION_LENGTH,
    MAX_BATCH_ADD_QUESTIONS,
    parse_question_input,
    is_admin,
)
from .modals import SuggestionModal
from .views import (
    BaseTimeoutView,
    QotdView,
    QotdPanelView,
    LanguageSelectView,
    SuggestionReviewButton,
)
from .database import QotdDatabaseMixin
from .embeds import QotdEmbedsMixin
from .scheduler import QotdSchedulerMixin


class Qotd(commands.Cog, QotdDatabaseMixin, QotdEmbedsMixin, QotdSchedulerMixin):
# Qotd Cog
# ---------------------------------------------------------------------------

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
        await self.db.execute("PRAGMA foreign_keys=ON")
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
    async def qotd_suggest(self, interaction: discord.Interaction):
        user_lang = self.get_user_language(interaction, interaction.guild_id)
        channels = await self.get_qotd_channels(interaction.guild_id)
        if not channels:
            await interaction.response.send_message(t(user_lang, "no_channels_configured"), ephemeral=True)
            return

        target_ch_id: Optional[int] = None
        if interaction.channel_id in [c["channel_id"] for c in channels]:
            target_ch_id = interaction.channel_id

        # The channel selection is done in the form
        modal = SuggestionModal(
            self,
            interaction.guild_id,
            channels=channels,
            target_channel_id=target_ch_id,
            lang=user_lang,
        )
        await interaction.response.send_modal(modal)

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
