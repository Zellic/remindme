"""
remindme Discord bot — Python port of the /remindme feature from discord-rook.

Slash commands:
  /remindme delay:<duration> [message:<text>]
  /remindat  at:<time>      [message:<text>]

Context-menu commands (right-click a message):
  "Remind me in 30 minutes"
  "Remind me in 5 hours"
  "Remind me in 1 day"
  "Remind me in 7 days"

Reminder DMs include snooze buttons (30 min / 1 h / 2 h / 4 h / 24 h).
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv
import redis as redis_lib

from datetime_parser import parse, ParseSuccess, ParseError
from reminder_manager import (
    AUTO_REMIND_STOP_PREFIX,
    AutoReminder,
    AutoReminderManager,
    DiscordMessage,
    Reminder,
    ReminderManager,
    ReminderType,
    SNOOZE_DURATIONS,
    SNOOZE_PREFIX,
)
from timeparse import human_readable_duration, parse_duration

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN       = os.environ["BOT_TOKEN"]
REDIS_URL       = os.environ.get("REDIS_URL", "redis://localhost:6379")
APPLICATION_KEY = os.environ.get("APPLICATION_KEY", "remindme-bot")
# Optional: set to a guild ID (int) for instant command registration during dev.
# Leave blank for global registration (takes up to an hour to propagate).
GUILD_ID_STR    = os.environ.get("GUILD_ID", "")
GUILD_OBJECT    = discord.Object(id=int(GUILD_ID_STR)) if GUILD_ID_STR else None


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
class RemindMeBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(
            self,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        )
        self.redis_client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
        self.reminder_manager      = ReminderManager(self.redis_client, APPLICATION_KEY)
        self.auto_reminder_manager = AutoReminderManager(self.redis_client, APPLICATION_KEY)

    async def setup_hook(self) -> None:
        self.reminder_manager.set_send_callback(self._deliver_reminder)
        self.reminder_manager.load_from_redis()

        self.auto_reminder_manager.set_send_callback(self._deliver_auto_reminder)
        self.auto_reminder_manager.load_from_redis()

        # Register persistent views/items so buttons survive restarts
        self.add_view(SnoozeView(reminder_manager=self.reminder_manager, original_source=None))
        self.add_dynamic_items(AutoRemindStopButton)

        if GUILD_OBJECT:
            self.tree.copy_global_to(guild=GUILD_OBJECT)
            await self.tree.sync(guild=GUILD_OBJECT)
            log.info("Commands synced to guild %s", GUILD_ID_STR)

        await self.tree.sync()
        log.info("Commands synced globally (enables DMs; may take up to 1h to propagate)")

        self.reminder_manager.start_checking()
        self.auto_reminder_manager.start_checking()
        log.info("Reminder checkers started.")

    async def _deliver_reminder(self, reminder: Reminder) -> None:
        user = await self.fetch_user(reminder.user_id)
        dm   = await user.create_dm()

        elapsed     = int(datetime.now(tz=timezone.utc).timestamp()) - reminder.src_time
        elapsed_str = human_readable_duration(timedelta(seconds=elapsed))

        body = f"Reminder set {elapsed_str} ago is due. Source channel: {reminder.source_location.as_link()}"
        if reminder.message:
            body += f"\n\n>>> {reminder.message}"

        view = SnoozeView(reminder_manager=self.reminder_manager, original_source=reminder.source_location)
        await dm.send(body, view=view)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def _deliver_auto_reminder(self, ar: AutoReminder) -> None:
        user = await self.fetch_user(ar.user_id)
        dm   = await user.create_dm()

        age     = int(datetime.now(tz=timezone.utc).timestamp()) - ar.created_at
        age_str = human_readable_duration(timedelta(seconds=age))
        interval_str = human_readable_duration(timedelta(seconds=ar.interval_seconds))

        body = (
            f"**Recurring reminder** (every {interval_str}) — started {age_str} ago.\n"
            f"Source channel: {ar.source_location.as_link()}"
        )
        if ar.message:
            body += f"\n\n>>> {ar.message}"

        view = AutoRemindStopView(ar.uuid)
        await dm.send(body, view=view)

    async def close(self) -> None:
        self.reminder_manager.stop_checking()
        self.auto_reminder_manager.stop_checking()
        await super().close()


bot = RemindMeBot()


# ---------------------------------------------------------------------------
# Snooze View (persistent buttons)
# ---------------------------------------------------------------------------
class SnoozeView(discord.ui.View):
    def __init__(self, *, reminder_manager: ReminderManager, original_source: Optional[DiscordMessage]):
        super().__init__(timeout=None)
        self._manager = reminder_manager
        self._source  = original_source

        for label, seconds in SNOOZE_DURATIONS:
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=SNOOZE_PREFIX + label,
            )
            btn.callback = self._make_callback(label, seconds)
            self.add_item(btn)

    def _make_callback(self, label: str, seconds: int):
        async def callback(interaction: discord.Interaction):
            # Preserve the original source through the snooze chain so the
            # link always points back to where the reminder was first created.
            source = self._source or DiscordMessage(
                channel_id=interaction.channel_id,
                guild_id=interaction.guild_id,
                message_id=interaction.message.id if interaction.message else None,
            )
            now_ts = int(datetime.now(tz=timezone.utc).timestamp())
            snooze_msg = (
                f"Re-triggering snoozed reminder because {human_readable_duration(timedelta(seconds=seconds))} "
                f"have elapsed. Original source channel: {source.as_link()}"
            )
            reminder = Reminder(
                type=ReminderType.SNOOZE,
                src_time=now_ts,
                due_time=now_ts + seconds,
                message=snooze_msg,
                user_id=interaction.user.id,
                source_location=source,
            )
            self._manager.add_reminder(reminder)
            await interaction.response.send_message(
                f"Reminder snoozed for {human_readable_duration(timedelta(seconds=seconds))}.",
                ephemeral=True,
            )
        return callback


# ---------------------------------------------------------------------------
# AutoRemind stop button (DynamicItem — one class handles all UUIDs)
# ---------------------------------------------------------------------------
class AutoRemindStopButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"autoremind_stop_(?P<uuid>[0-9a-f\-]+)",
):
    def __init__(self, reminder_uuid: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Stop this reminder",
                style=discord.ButtonStyle.danger,
                custom_id=f"{AUTO_REMIND_STOP_PREFIX}{reminder_uuid}",
            )
        )
        self.reminder_uuid = reminder_uuid

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match,
    ) -> "AutoRemindStopButton":
        return cls(match.group("uuid"))

    async def callback(self, interaction: discord.Interaction) -> None:
        stopped = bot.auto_reminder_manager.cancel(self.reminder_uuid, interaction.user.id)
        if stopped:
            await interaction.response.send_message(
                "Auto-reminder stopped. You won't receive this reminder again.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Reminder not found — it may have already been stopped.",
                ephemeral=True,
            )


class AutoRemindStopView(discord.ui.View):
    """Single-button view attached to every auto-reminder DM."""
    def __init__(self, reminder_uuid: str) -> None:
        super().__init__(timeout=None)
        self.add_item(AutoRemindStopButton(reminder_uuid))


# ---------------------------------------------------------------------------
# /autoremind  command group
# ---------------------------------------------------------------------------
class AutoRemindGroup(app_commands.Group, name="autoremind", description="Manage recurring reminders."):

    @app_commands.command(name="start", description="Start a recurring reminder that fires every N days (or any duration).")
    @app_commands.describe(
        interval="How often to repeat, e.g. '7d', '2 weeks', '12h'",
        message="What you wish to be reminded about",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        interval: str,
        message: str,
    ) -> None:
        try:
            td = parse_duration(interval)
        except ValueError as exc:
            await interaction.response.send_message(f"Invalid interval: {exc}", ephemeral=True)
            return

        if td.total_seconds() < 86400:
            await interaction.response.send_message(
                "Interval must be at least 1 day.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        source = await _source_location(interaction)

        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        interval_sec = int(td.total_seconds())
        ar = AutoReminder(
            interval_seconds=interval_sec,
            message=message,
            user_id=interaction.user.id,
            source_location=source,
            next_due_time=now_ts + interval_sec,
            created_at=now_ts,
        )
        bot.auto_reminder_manager.add(ar)

        interval_str = human_readable_duration(td)
        await interaction.followup.send(
            f"Auto-reminder set. You'll be reminded every {interval_str}.\n"
            f"ID: `{ar.uuid[:8]}` (use `/autoremind stop` to cancel).",
            ephemeral=True,
        )

    @app_commands.command(name="list", description="List your active recurring reminders.")
    async def list_reminders(self, interaction: discord.Interaction) -> None:
        reminders = bot.auto_reminder_manager.list_for_user(interaction.user.id)
        if not reminders:
            await interaction.response.send_message(
                "You have no active auto-reminders.", ephemeral=True
            )
            return

        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        lines: list[str] = [f"**Your auto-reminders** ({len(reminders)} active)"]
        for ar in reminders:
            interval_str = human_readable_duration(timedelta(seconds=ar.interval_seconds))
            secs_until   = max(0, ar.next_due_time - now_ts)
            next_str     = human_readable_duration(timedelta(seconds=secs_until))
            preview      = ar.message[:60] + ("…" if len(ar.message) > 60 else "")
            lines.append(
                f"`{ar.uuid[:8]}` · every {interval_str} · next in {next_str} · \"{preview}\""
            )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="stop", description="Stop a recurring reminder by its ID.")
    @app_commands.describe(id="The 8-character ID shown in /autoremind list")
    async def stop_reminder(self, interaction: discord.Interaction, id: str) -> None:
        reminders = bot.auto_reminder_manager.list_for_user(interaction.user.id)
        match = next((ar for ar in reminders if ar.uuid.startswith(id.lower())), None)
        if match is None:
            await interaction.response.send_message(
                f"No auto-reminder found with ID `{id}`. Use `/autoremind list` to see your active reminders.",
                ephemeral=True,
            )
            return

        bot.auto_reminder_manager.cancel(match.uuid, interaction.user.id)
        await interaction.response.send_message(
            f"Auto-reminder `{id}` stopped.", ephemeral=True
        )


bot.tree.add_command(AutoRemindGroup())


# ---------------------------------------------------------------------------
# Helper: resolve source location for a slash command interaction
# ---------------------------------------------------------------------------
async def _source_location(interaction: discord.Interaction) -> DiscordMessage:
    """Return a DiscordMessage pointing to the last non-bot message in the channel."""
    guild_id = interaction.guild_id
    channel  = interaction.channel

    last_msg_id: Optional[int] = None
    if channel and hasattr(channel, "history"):
        try:
            async for msg in channel.history(limit=20):
                if not msg.author.bot:
                    last_msg_id = msg.id
                    break
        except discord.Forbidden:
            pass

    return DiscordMessage(
        channel_id=interaction.channel_id,
        guild_id=guild_id,
        message_id=last_msg_id,
    )


# ---------------------------------------------------------------------------
# /listreminders
# ---------------------------------------------------------------------------
@bot.tree.command(name="listreminders", description="List all your pending reminders and auto-reminders.")
async def listreminders(interaction: discord.Interaction) -> None:
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    uid = interaction.user.id
    lines: list[str] = []

    one_shots = [r for r in bot.reminder_manager._reminders if r.user_id == uid]
    if one_shots:
        lines.append("**One-shot reminders**")
        for r in sorted(one_shots, key=lambda r: r.due_time):
            secs_until = max(0, r.due_time - now_ts)
            preview = f' — "{r.message[:50]}{"…" if len(r.message) > 50 else ""}"' if r.message else ""
            lines.append(f"• in {human_readable_duration(timedelta(seconds=secs_until))}{preview} — {r.source_location.as_link()}")

    autos = bot.auto_reminder_manager.list_for_user(uid)
    if autos:
        lines.append("**Auto-reminders**")
        for ar in sorted(autos, key=lambda ar: ar.next_due_time):
            secs_until    = max(0, ar.next_due_time - now_ts)
            interval_str  = human_readable_duration(timedelta(seconds=ar.interval_seconds))
            preview = f' — "{ar.message[:50]}{"…" if len(ar.message) > 50 else ""}"' if ar.message else ""
            lines.append(f"• `{ar.uuid[:8]}` every {interval_str}, next in {human_readable_duration(timedelta(seconds=secs_until))}{preview} — {ar.source_location.as_link()}")

    if not lines:
        await interaction.response.send_message("You have no pending reminders.", ephemeral=True)
        return

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# /remindme  delay:<duration>  [message:<text>]
# ---------------------------------------------------------------------------
@bot.tree.command(name="remindme", description="Set a reminder after a delay (e.g. 2h 30m).")
@app_commands.describe(
    delay="How long until this reminder fires (e.g. '2h 30m', '1 day', '90m')",
    message="What you wish to be reminded about (optional)",
)
async def remindme(interaction: discord.Interaction, delay: str, message: Optional[str] = None):
    try:
        td = parse_duration(delay)
    except ValueError as exc:
        await interaction.response.send_message(f"Invalid delay: {exc}", ephemeral=True)
        return

    if td.total_seconds() < 1:
        await interaction.response.send_message("Delay must be at least 1 second.", ephemeral=True)
        return

    now_ts  = int(datetime.now(tz=timezone.utc).timestamp())
    due_ts  = now_ts + int(td.total_seconds())
    ack_msg = f"Reminder scheduled in {human_readable_duration(td)}."

    await interaction.response.defer(ephemeral=True)
    source = await _source_location(interaction)

    bot.reminder_manager.add_reminder(Reminder(
        type=ReminderType.COMMAND,
        src_time=now_ts,
        due_time=due_ts,
        message=message or "",
        user_id=interaction.user.id,
        source_location=source,
    ))
    await interaction.followup.send(ack_msg, ephemeral=True)


# ---------------------------------------------------------------------------
# /remindat  at:<time>  [message:<text>]
# ---------------------------------------------------------------------------
@bot.tree.command(name="remindat", description="Set a reminder for a specific time (e.g. '5pm EDT tomorrow').")
@app_commands.describe(
    at="The time the reminder should fire, e.g. '5pm EDT tomorrow', '17:00 PST friday'",
    message="What you wish to be reminded about (optional)",
)
async def remindat(interaction: discord.Interaction, at: str, message: Optional[str] = None):
    result = parse(at)

    if isinstance(result, ParseError):
        await interaction.response.send_message(f"Bad timestamp — {result.message}", ephemeral=True)
        return

    # result is ParseSuccess
    if result.zone_id is None:
        await interaction.response.send_message(
            "Please include a timezone in your time string (e.g. `5pm EDT tomorrow`).",
            ephemeral=True,
        )
        return

    now      = datetime.now(tz=timezone.utc)
    due_dt   = result.to_datetime(result.zone_id).astimezone(timezone.utc)

    # If the time is in the past, advance by 1 day
    if due_dt <= now:
        due_dt = due_dt + timedelta(days=1)

    delay_td = due_dt - now
    now_ts  = int(now.timestamp())
    due_ts  = int(due_dt.timestamp())
    ack_msg = f"Reminder scheduled in {human_readable_duration(delay_td)}."

    await interaction.response.defer(ephemeral=True)
    source = await _source_location(interaction)

    bot.reminder_manager.add_reminder(Reminder(
        type=ReminderType.COMMAND,
        src_time=now_ts,
        due_time=due_ts,
        message=message or "",
        user_id=interaction.user.id,
        source_location=source,
    ))
    await interaction.followup.send(ack_msg, ephemeral=True)


# ---------------------------------------------------------------------------
# Context-menu commands
# ---------------------------------------------------------------------------
def _make_context_menu(label: str, seconds: int):
    @bot.tree.context_menu(name=label)
    async def _cmd(interaction: discord.Interaction, msg: discord.Message):
        await interaction.response.defer(ephemeral=True)
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        source = DiscordMessage(
            channel_id=msg.channel.id,
            guild_id=interaction.guild_id,
            message_id=msg.id,
        )
        bot.reminder_manager.add_reminder(Reminder(
            type=ReminderType.CONTEXT_MENU,
            src_time=now_ts,
            due_time=now_ts + seconds,
            message="Triggered by context menu.",
            user_id=interaction.user.id,
            source_location=source,
        ))
        await interaction.followup.send(
            f"Reminder scheduled in {human_readable_duration(timedelta(seconds=seconds))}.",
            ephemeral=True,
        )


_make_context_menu("Remind me in 30 minutes", 30 * 60)
_make_context_menu("Remind me in 5 hours",    5 * 3600)
_make_context_menu("Remind me in 1 day",      24 * 3600)
_make_context_menu("Remind me in 7 days",     7 * 24 * 3600)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
