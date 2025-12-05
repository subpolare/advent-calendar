# Advent Calendar Bot

Telegram bot that manages an Advent calendar schedule for the TOПЛЕС community. Admins schedule multimedia posts inside the admin chat, and the bot delivers a new episode every day at 19:00 to subscribed users.

## Features
- `/init` for the admin chat to capture the very first post (reply with photo/video); new subscribers receive this message between the greeting and the subscription reminder.
- `/set` for the admin chat to queue a scheduled post via reply with a photo or video (caption optional); the bot records the caption text and the original message ID.
- `/id` helper command that replies with the current chat ID (and user ID in private chats) so you can configure the admin chat.
- Stores queued posts in `storage/posts.tsv` (tab-separated `datetime\ttext\tmessage_id`) so it can later copy the exact admin message without re-uploading media.
- Keeps track of subscribed users in a PostgreSQL database `users` (table `users(user_id, username, status)`), persisted under `storage/users-db` when run through Docker.
- User flow:
  - First-time `/start` now runs an interactive funnel: greeting → inline question → `/init` media drop → follow-up question; choosing “Не хочу” immediately toggles the user to `stop` status, while “Давай” confirms the daily series.
  - `/stop` pauses deliveries with a confirmation message; `/start` after `/stop` re-enables sending with a comeback message.
  - Already active `/start` calls receive a reassurance message about the next drop; `/stop` when already stopped sends the penguin reminder.
- Daily content is delivered by copying the stored admin message to each active user (so there is no "forwarded" label) once its `datetime` is reached.
- Background worker wakes up every minute, reads the TSV table, and sends any post whose timestamp is due; dispatched posts are recorded in `storage/sent.log` to avoid duplicates even across restarts.

## Requirements
- Python 3.11+
- PostgreSQL 15 (or compatible). A helper service is provided via Docker Compose.

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment variables
Create a `.env` file (see `.env.example`) with:

```
BOT_TOKEN=<telegram bot token>
ADMIN_CHAT_ID=<numeric admin chat id>
```

`ADMIN_CHAT_ID` is optional while you are discovering the ID via `/id`, but the scheduling commands stay disabled until it is set.

Optional overrides:
- `DATABASE_URL` – defaults to `postgresql://postgres:postgres@localhost:5432/users` (matches the docker-compose service).
- `BOT_TIMEZONE` – defaults to `Europe/Moscow`.
- `STORAGE_DIR` – defaults to `storage`.

## Running PostgreSQL locally
Start the bundled database (data persisted under `storage/users-db`):

```bash
docker compose up -d postgres
```

The bot automatically creates the `users` table inside the `users` database.

## Running the bot

### Native Python environment

```bash
python bot.py
```

### Docker Compose
Build the bot image (uses the included Dockerfile) and run it together with PostgreSQL. Variables come from `.env` plus the overrides in `docker-compose.yml`.

```bash
docker compose up -d bot
```

Follow logs with:

```bash
docker compose logs -f bot
```

### What the bot does
1. Registers handlers, expects the admin to configure `/init` once, and runs the minute-by-minute publisher (still aiming for 19:00 posts by default).
2. Listens to commands/messages from users and the admin.
3. Stores metadata under `storage/posts.tsv` and copies each stored admin message (including the `/init` intro post) to subscribers when it becomes due.

### Admin workflow
1. In the admin chat (defined by `ADMIN_CHAT_ID`), run `/init` and reply to the bot’s prompt with the welcome media message that should be shown to every newcomer.
2. Use `/set` and reply with future posts to fill the schedule from 3 Dec 2025 through 31 Dec 2025.

## Data files
- `storage/posts.tsv` – queued posts with timestamps, captions, and message IDs.
- `storage/initial_post.json` – metadata for the `/init` message that every new subscriber receives right after the greeting.
- `storage/users-db/` – Postgres data directory when using the provided Compose service.
- `storage/sent.log` – ISO timestamps of already-published posts so the minute-by-minute worker does not resend them.
