# remindme

A standalone Discord bot for setting one-shot and recurring reminders.

## Features

### One-shot reminders
- `/remindme delay:<duration>` — fire once after a delay (`2h 30m`, `7d`, `90m`, etc.)
- `/remindat at:<time>` — fire at a specific time (`5pm EDT tomorrow`, `17:00 PST friday`, etc.)
- Right-click any message → **Remind me in 30 minutes / 5 hours / 1 day / 7 days**
- Reminders are delivered as DMs with snooze buttons (30 min, 1 h, 2 h, 4 h, 24 h)
- Snooze chains preserve the original source link

### Recurring reminders
- `/autoremind start interval:<duration> message:<text>` — repeats indefinitely every N days (minimum 1 day)
- `/autoremind list` — shows all active recurring reminders with next fire time
- `/autoremind stop id:<id>` — cancel by the 8-character ID shown in list
- Each recurring reminder DM includes a **Stop this reminder** button

### Listing
- `/listreminders` — shows all pending one-shot and recurring reminders with time until next fire and source link

### Works anywhere
The bot is installable as a user app, so commands work in DMs with the bot, DMs between users, group DMs, and servers.

## Setup

### 1. Create a Discord application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and create a new application
2. Under **Bot**, create a bot and copy the token
3. Under **Installation**, enable **User Install** as an install type
4. Under **OAuth2**, add the bot to your server with the `bot` and `applications.commands` scopes

### 2. Configure environment

```sh
cp .env.example .env
```

Edit `.env`:

```ini
BOT_TOKEN=your_discord_bot_token

# Full Redis connection URL including port and credentials
REDIS_URL=redis://:password@your-redis-host:6379

# Namespace prefix for Redis keys — change this if sharing a Redis instance
# with another bot to avoid key collisions
APPLICATION_KEY=remindme

# Optional: Guild ID for instant command registration during development.
# Leave blank for global registration (takes up to 1 hour to propagate,
# but required for DM and user-install support).
GUILD_ID=
```

### 3. Run

**With uv (local):**
```sh
uv run bot.py
```

**With Docker Compose (production):**
```sh
docker compose up -d
```

**With Docker Compose + local Redis (for testing without an external Redis):**
```sh
docker compose --profile need_redis up -d
```

The local Redis is exposed on port `6380` to avoid clashing with anything already on `6379`. Set `REDIS_URL=redis://redis:6379` in `.env` when using the compose Redis service (Docker networking uses the service name).

## Sharing a Redis instance

If you're running alongside another bot on the same Redis, set a distinct `APPLICATION_KEY` in `.env`. All keys are namespaced under this prefix:

```
<APPLICATION_KEY>:remindme:<uuid>     # one-shot reminders
<APPLICATION_KEY>:autoremind:<uuid>   # recurring reminders
```

Two bots with different `APPLICATION_KEY` values coexist without interference.

## Duration format

Both `/remindme` and `/autoremind start` accept flexible duration strings:

| Input | Meaning |
|---|---|
| `30m` | 30 minutes |
| `2h 30m` | 2 hours 30 minutes |
| `1d` | 1 day |
| `2 weeks` | 2 weeks |
| `90` | 90 seconds |

## Time format (`/remindat`)

Accepts natural time strings with optional timezone and day:

| Input | Meaning |
|---|---|
| `5pm EDT` | 5:00 PM Eastern today (or tomorrow if past) |
| `17:00 PST friday` | 5:00 PM Pacific next Friday |
| `9am EST tomorrow` | 9:00 AM Eastern tomorrow |

Supported timezone abbreviations: `EST`, `EDT`, `CST`, `CDT`, `MST`, `MDT`, `PST`, `PDT`, `GMT`, `UTC`, `BST`, `CET`, `CEST`, `EET`, `EEST`, `WET`, `WEST`. Full IANA names (e.g. `Europe/Berlin`) also work.

## Architecture

| File | Purpose |
|---|---|
| `bot.py` | Discord bot, slash commands, context menus, button views |
| `reminder_manager.py` | In-memory state + Redis persistence for both reminder types |
| `timeparse.py` | Duration string parser |
| `datetime_parser.py` | Natural language time/date parser |

Reminders are stored in Redis individually as JSON so they survive restarts. The bot polls for due reminders every second. On startup all reminders are reloaded from Redis.

## Requirements

- Python 3.12+
- Redis
- [uv](https://github.com/astral-sh/uv)
