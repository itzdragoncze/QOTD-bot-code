from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
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
    DEFAULT_SCHEDULED_TIME,
    MAX_QUEUE_QUESTIONS,
    MAX_TOTAL_QUESTIONS,
    MAX_PENDING_SUGGESTIONS,
    format_discord_timestamp,
    get_next_qotd_datetime,
    Icon,
)


class QotdEmbedsMixin:
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
            source_val = f"{Icon.BULB} {row['suggested_by_name']}"
        elif row.get("added_by_name"):
            source_val = f"{Icon.ACCOUNT} {row['added_by_name']}"
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
        if row.get("message"):
            embed.add_field(name=t(lang, "sugg_detail_note_label"), value=row["message"], inline=False)
        target_ch_id = row.get("target_channel_id")
        if target_ch_id:
            embed.add_field(name=t(lang, "sugg_target_channel"), value=f"<#{target_ch_id}>", inline=True)
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

        sug_role_id = guild_settings.get("suggest_role_id")
        sug_role_str = f"<@&{sug_role_id}>" if sug_role_id else t(effective_lang, "settings_everyone")
        embed.add_field(name=f"{Icon.BULB} " + t(effective_lang, "settings_suggest_role"), value=sug_role_str, inline=True)

        if channels:
            chan_lines = []
            for idx, c in enumerate(channels, 1):
                cid = c["channel_id"]
                sched = c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME
                role_str = f"<@&{c['role_id']}>" if c.get("role_id") else t(effective_lang, "settings_not_used")
                q_cnt = await self.get_queue_count(guild_id, channel_id=cid)
                chan_max = c.get("max_queue_limit") or MAX_QUEUE_QUESTIONS
                chan_lines.append(f"`{idx}.` <#{cid}> • {Icon.SCHEDULE} **{sched}** | {Icon.NOTIFICATIONS} {role_str} | {Icon.SCHEDULE_PENDING} Queue: **{q_cnt}/{chan_max}**")
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

        total_max_queue = sum((c.get("max_queue_limit") or MAX_QUEUE_QUESTIONS) for c in channels) if channels else MAX_QUEUE_QUESTIONS

        embed.add_field(
            name=t(effective_lang, "settings_db_status"),
            value=t(effective_lang, "settings_db_status_val", queue=c1, max_queue=total_max_queue, history=c2, total=c1 + c2, max_total=MAX_TOTAL_QUESTIONS, sugg=c3, max_sugg=MAX_PENDING_SUGGESTIONS),
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

        embed.add_field(name=f"{Icon.CAMPAIGN} " + t(effective_lang, "channel_label"), value=f"<#{channel_id}>", inline=True)
        sched = c.get("scheduled_time") or DEFAULT_SCHEDULED_TIME
        embed.add_field(name=f"{Icon.SCHEDULE} " + t(effective_lang, "btn_manage_channel_hour"), value=f"**{sched}** (Europe/Prague)", inline=True)
        role_str = f"<@&{c['role_id']}>" if c.get("role_id") else t(effective_lang, "settings_not_used")
        embed.add_field(name=f"{Icon.NOTIFICATIONS} " + t(effective_lang, "btn_manage_channel_role"), value=role_str, inline=True)

        thresh = c.get("low_queue_threshold") or 3
        embed.add_field(name=f"{Icon.SCHEDULE_PENDING} " + t(effective_lang, "btn_manage_channel_threshold"), value=f"**{thresh}** questions", inline=True)

        q_count = await self.get_queue_count(guild_id, channel_id=channel_id)
        h_count = await self.get_total_question_count(guild_id, channel_id=channel_id) - q_count
        embed.add_field(name=f"{Icon.HARD_DRIVE} Queue / History", value=f"Queue: **{q_count}** | History: **{h_count}**", inline=True)

        last_date = c.get("last_posted_date") or "None"
        embed.add_field(name=f"{Icon.CALENDAR_MONTH} Last Posted Date", value=f"`{last_date}`", inline=True)
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
        top_users = await cursor.fetchall()

        embed = discord.Embed(title=t(lang, "top_title"), colour=COLOR_LEADERBOARD)
        if not top_users:
            embed.description = t(lang, "top_empty")
        else:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for idx, row in enumerate(top_users):
                badge = medals[idx] if idx < len(medals) else f"`{idx + 1}.`"
                lines.append(t(lang, "top_entry", rank=badge, user_id=row["suggested_by_id"], name=row["suggested_by_name"], count=row["accepted_count"]))
            embed.add_field(name=t(lang, "top_header"), value="\n".join(lines), inline=False)

        embed.set_footer(text=t(lang, "top_footer"))
        return embed

    async def build_info_embed(self, guild_id: int, lang: Optional[str] = None) -> discord.Embed:
        effective_lang = lang or self.get_user_language(None, guild_id)
        channels = await self.get_qotd_channels(guild_id)

        sched_time = DEFAULT_SCHEDULED_TIME
        if len(channels) == 1:
            sched_time = channels[0].get("scheduled_time") or DEFAULT_SCHEDULED_TIME

        desc = t(effective_lang, "info_desc", time=sched_time)
        if len(channels) == 1:
            next_dt = get_next_qotd_datetime(channels[0])
            ts = int(next_dt.timestamp())
            desc += f"\n\n{Icon.SCHEDULE} **" + t(effective_lang, "info_next_post") + f"**: <t:{ts}:t> (<t:{ts}:R>)"

        embed = discord.Embed(
            title=t(effective_lang, "info_title"),
            description=desc,
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
                next_dt = get_next_qotd_datetime(c)
                ts = int(next_dt.timestamp())
                ch_lines.append(f"• {ch_name} — **{sched}** • <t:{ts}:t> (<t:{ts}:R>)")
            embed.add_field(
                name=f"{Icon.CALENDAR_MONTH} " + t(effective_lang, "available_qotd_channels_label"),
                value="\n".join(ch_lines),
                inline=False,
            )

        embed.set_footer(text=t(effective_lang, "info_footer"))
        return embed

    # -----------------------------------------------------------------------
    # Navigation Handler
    # -----------------------------------------------------------------------

