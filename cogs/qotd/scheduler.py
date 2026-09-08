import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict
import discord
from discord.ext import tasks
from translations import t
from .constants import (
    logger,
    COLOR_POST,
    COLOR_DANGER,
    COLOR_SUGGESTIONS,
    DEFAULT_SCHEDULED_TIME,
    QOTD_TIMEZONE,
    MAX_QUESTION_LENGTH,
)
from .views import QotdView


class QotdSchedulerMixin:
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
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as err:
                logger.warning("Could not fetch admin channel %s for low queue warning: %s", admin_channel_id, err)
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
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as err:
                    logger.error("Could not fetch QOTD channel %s: %s", channel_id, err)
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
                qid = await self.add_question(
                    guild_id,
                    final_question,
                    source="manual",
                    bypass_queue_limit=True,
                    channel_id=channel_id,
                    bypass_total_limit=True,
                )
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
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as err:
                        logger.debug("Could not fetch attribution user %s: %s", attr_user_id, err)

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
                except (ValueError, TypeError):
                    is_due = (now.hour >= 9)
            else:
                try:
                    s_hour = int(scheduled_raw)
                    if now.hour >= s_hour:
                        is_due = True
                except (ValueError, TypeError):
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
