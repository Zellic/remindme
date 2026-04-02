import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional

import redis as redis_lib

log = logging.getLogger(__name__)

SNOOZE_PREFIX = "snooze_"
SNOOZE_DURATIONS: list[tuple[str, int]] = [
    ("Snooze: 30 minutes", 30 * 60),
    ("Snooze: 1 hour",     60 * 60),
    ("Snooze: 2 hours",    2 * 3600),
    ("Snooze: 4 hours",    4 * 3600),
    ("Snooze: 24 hours",   24 * 3600),
]


class ReminderType(str, Enum):
    COMMAND      = "reminder_command"
    CONTEXT_MENU = "reminder_context"
    SNOOZE       = "reminder_snooze"


@dataclass
class DiscordMessage:
    channel_id: int
    guild_id: Optional[int] = None
    message_id: Optional[int] = None

    def as_link(self) -> str:
        guild = str(self.guild_id) if self.guild_id else "@me"
        base = f"https://discord.com/channels/{guild}/{self.channel_id}"
        return f"{base}/{self.message_id}" if self.message_id else base

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "guild_id":   self.guild_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DiscordMessage":
        return cls(
            channel_id=d["channel_id"],
            guild_id=d.get("guild_id"),
            message_id=d.get("message_id"),
        )


@dataclass
class Reminder:
    type: ReminderType
    src_time: int        # epoch seconds
    due_time: int        # epoch seconds
    message: str
    user_id: int
    source_location: DiscordMessage
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "type":            self.type.value,
            "src_time":        self.src_time,
            "due_time":        self.due_time,
            "message":         self.message,
            "user_id":         self.user_id,
            "source_location": self.source_location.to_dict(),
            "uuid":            self.uuid,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Reminder":
        return cls(
            type=ReminderType(d["type"]),
            src_time=d["src_time"],
            due_time=d["due_time"],
            message=d["message"],
            user_id=d["user_id"],
            source_location=DiscordMessage.from_dict(d["source_location"]),
            uuid=d["uuid"],
        )


# Signature: async (reminder: Reminder) -> None
SendCallback = Callable[["Reminder"], Awaitable[None]]

AUTO_REMIND_STOP_PREFIX = "autoremind_stop_"


@dataclass
class AutoReminder:
    interval_seconds: int   # how often to repeat
    message: str
    user_id: int
    source_location: DiscordMessage
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    next_due_time: int = 0  # epoch seconds; set to now+interval on creation
    created_at: int = field(
        default_factory=lambda: int(datetime.now(tz=timezone.utc).timestamp())
    )

    def to_dict(self) -> dict:
        return {
            "interval_seconds": self.interval_seconds,
            "message":          self.message,
            "user_id":          self.user_id,
            "source_location":  self.source_location.to_dict(),
            "uuid":             self.uuid,
            "next_due_time":    self.next_due_time,
            "created_at":       self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutoReminder":
        return cls(
            interval_seconds=d["interval_seconds"],
            message=d["message"],
            user_id=d["user_id"],
            source_location=DiscordMessage.from_dict(d["source_location"]),
            uuid=d["uuid"],
            next_due_time=d["next_due_time"],
            created_at=d["created_at"],
        )


AutoSendCallback = Callable[["AutoReminder"], Awaitable[None]]


class AutoReminderManager:
    def __init__(self, redis_client: redis_lib.Redis, application_key: str):
        self._redis    = redis_client
        self._base_key = f"{application_key}:autoremind"
        self._reminders: list[AutoReminder] = []
        self._lock     = asyncio.Lock()
        self._send_cb: Optional[AutoSendCallback] = None
        self._checker_task: Optional[asyncio.Task] = None

    def set_send_callback(self, cb: AutoSendCallback) -> None:
        self._send_cb = cb

    def load_from_redis(self) -> None:
        count = 0
        for key in self._redis.keys(f"{self._base_key}:*"):
            try:
                raw = self._redis.get(key)
                if raw:
                    self._reminders.append(AutoReminder.from_dict(json.loads(raw)))
                    count += 1
            except Exception:
                log.exception("Failed to reload auto-reminder from Redis key %s", key)
        log.info("Loaded %d auto-reminders from Redis.", count)

    def _redis_key(self, reminder_uuid: str) -> str:
        return f"{self._base_key}:{reminder_uuid}"

    def add(self, ar: AutoReminder) -> None:
        self._reminders.append(ar)
        self._redis.set(self._redis_key(ar.uuid), json.dumps(ar.to_dict()))
        log.info(
            "Added auto-reminder %s for user %d, interval=%ds, first due at %s",
            ar.uuid, ar.user_id, ar.interval_seconds,
            datetime.fromtimestamp(ar.next_due_time, tz=timezone.utc).isoformat(),
        )

    def cancel(self, reminder_uuid: str, user_id: int) -> bool:
        """Stop an auto-reminder. Returns True if it was found and removed."""
        for ar in self._reminders:
            if ar.uuid == reminder_uuid and ar.user_id == user_id:
                self._reminders.remove(ar)
                self._redis.delete(self._redis_key(reminder_uuid))
                log.info("Cancelled auto-reminder %s for user %d", reminder_uuid, user_id)
                return True
        return False

    def list_for_user(self, user_id: int) -> list["AutoReminder"]:
        return [ar for ar in self._reminders if ar.user_id == user_id]

    async def _process(self) -> None:
        now = int(datetime.now(tz=timezone.utc).timestamp())

        async with self._lock:
            due = [ar for ar in self._reminders if ar.next_due_time <= now]

        for ar in due:
            try:
                if self._send_cb:
                    await self._send_cb(ar)
            except Exception:
                log.exception("Failed to send auto-reminder %s", ar.uuid)

            # Reschedule whether or not send succeeded — always advance the clock
            ar.next_due_time = now + ar.interval_seconds
            try:
                self._redis.set(self._redis_key(ar.uuid), json.dumps(ar.to_dict()))
            except Exception:
                log.exception("Failed to persist rescheduled auto-reminder %s", ar.uuid)

    def start_checking(self) -> None:
        if self._checker_task is not None:
            raise RuntimeError("Already started.")
        self._checker_task = asyncio.create_task(self._checker_loop())

    def stop_checking(self) -> None:
        if self._checker_task:
            self._checker_task.cancel()
            self._checker_task = None

    async def _checker_loop(self) -> None:
        while True:
            try:
                await self._process()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected error in auto-reminder checker loop")
            await asyncio.sleep(1)


class ReminderManager:
    def __init__(self, redis_client: redis_lib.Redis, application_key: str):
        self._redis    = redis_client
        self._base_key = f"{application_key}:remindme"
        self._reminders: list[Reminder] = []
        self._lock     = asyncio.Lock()
        self._send_cb: Optional[SendCallback] = None
        self._checker_task: Optional[asyncio.Task] = None

    def set_send_callback(self, cb: SendCallback) -> None:
        """Register the coroutine that delivers a due reminder to the user."""
        self._send_cb = cb

    def load_from_redis(self) -> None:
        count = 0
        for key in self._redis.keys(f"{self._base_key}:*"):
            try:
                raw = self._redis.get(key)
                if raw:
                    self._reminders.append(Reminder.from_dict(json.loads(raw)))
                    count += 1
            except Exception:
                log.exception("Failed to reload reminder from Redis key %s", key)
        log.info("Loaded %d reminders from Redis.", count)

    def _redis_key(self, reminder_uuid: str) -> str:
        return f"{self._base_key}:{reminder_uuid}"

    def add_reminder(self, reminder: Reminder) -> None:
        self._reminders.append(reminder)
        self._redis.set(self._redis_key(reminder.uuid), json.dumps(reminder.to_dict()))
        log.info(
            "Added reminder %s for user %d due at %s",
            reminder.uuid,
            reminder.user_id,
            datetime.fromtimestamp(reminder.due_time, tz=timezone.utc).isoformat(),
        )

    async def _process_reminders(self) -> None:
        now = int(datetime.now(tz=timezone.utc).timestamp())

        async with self._lock:
            due = [r for r in self._reminders if r.due_time <= now]
            for r in due:
                self._reminders.remove(r)

        for reminder in due:
            try:
                if self._send_cb:
                    await self._send_cb(reminder)
            except Exception:
                log.exception("Failed to send reminder %s", reminder.uuid)

            try:
                self._redis.delete(self._redis_key(reminder.uuid))
            except Exception:
                log.exception("Failed to delete reminder %s from Redis", reminder.uuid)

    def start_checking(self) -> None:
        if self._checker_task is not None:
            raise RuntimeError("Already started.")
        self._checker_task = asyncio.create_task(self._checker_loop())

    def stop_checking(self) -> None:
        if self._checker_task:
            self._checker_task.cancel()
            self._checker_task = None

    async def _checker_loop(self) -> None:
        while True:
            try:
                await self._process_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected error in reminder checker loop")
            await asyncio.sleep(1)
