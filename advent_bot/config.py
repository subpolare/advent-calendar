from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_id: Optional[int]
    storage_dir: Path
    posts_file: Path
    sent_log_file: Path
    initial_post_file: Path
    timezone: ZoneInfo
    publish_time: time
    schedule_start: date
    schedule_end: date
    database_dsn: str

    @staticmethod
    def load() -> "Config":
        token = os.environ.get("BOT_TOKEN")
        if not token:
            raise RuntimeError("BOT_TOKEN is not set in the environment")

        admin_chat = os.environ.get("ADMIN_CHAT_ID")
        admin_chat_id: Optional[int] = None
        if admin_chat:
            try:
                admin_chat_id = int(admin_chat)
            except ValueError as exc:
                raise RuntimeError("ADMIN_CHAT_ID must be an integer") from exc
        else:
            logger.warning("ADMIN_CHAT_ID is not set; admin commands are disabled")

        timezone_name = os.environ.get("BOT_TIMEZONE", "Europe/Moscow")
        try:
            tz = ZoneInfo(timezone_name)
        except Exception as exc:  # pragma: no cover - configuration error
            raise RuntimeError(f"Unknown timezone: {timezone_name}") from exc

        storage_dir = Path(os.environ.get("STORAGE_DIR", "storage"))
        storage_dir.mkdir(parents=True, exist_ok=True)
        posts_file = storage_dir / "posts.tsv"
        if not posts_file.exists():
            posts_file.write_text("datetime\ttext\tmessage_id\n", encoding="utf-8")
        sent_log_file = storage_dir / "sent.log"
        sent_log_file.touch(exist_ok=True)
        initial_post_file = storage_dir / "initial_post.json"
        if not initial_post_file.exists():
            initial_post_file.write_text("{}", encoding="utf-8")

        publish_time = time(hour=19, minute=0, tzinfo=tz)
        schedule_start = date(2025, 12, 3)
        schedule_end = date(2025, 12, 31)
        database_dsn = os.environ.get(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/users"
        )

        return Config(
            bot_token=token,
            admin_chat_id=admin_chat_id,
            storage_dir=storage_dir,
            posts_file=posts_file,
            sent_log_file=sent_log_file,
            initial_post_file=initial_post_file,
            timezone=tz,
            publish_time=publish_time,
            schedule_start=schedule_start,
            schedule_end=schedule_end,
            database_dsn=database_dsn,
        )


CONFIG = Config.load()
