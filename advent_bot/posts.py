from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class ScheduledPost:
    run_at: datetime
    text: str
    message_id: int


class PostStorage:
    def __init__(
        self,
        posts_file: Path,
        sent_log_file: Path,
        publish_hour: int = 19,
    ) -> None:
        self.posts_file = posts_file
        self.publish_hour = publish_hour
        self.sent_log_file = sent_log_file
        self._sent_cache = self._load_sent_cache()

    def _load_sent_cache(self) -> set[str]:
        if not self.sent_log_file.exists():
            return set()
        with self.sent_log_file.open("r", encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}

    def load_posts(self) -> list[ScheduledPost]:
        posts: list[ScheduledPost] = []
        if not self.posts_file.exists():
            return posts

        with self.posts_file.open("r", encoding="utf-8") as handle:
            reader = csv.reader(handle, delimiter="\t")
            for row in reader:
                if not row or row[0] == "datetime":
                    continue
                try:
                    run_at = datetime.fromisoformat(row[0])
                except ValueError:
                    continue
                text = row[1]
                try:
                    message_id = int(row[2])
                except (IndexError, ValueError):
                    continue
                posts.append(ScheduledPost(run_at=run_at, text=text, message_id=message_id))
        posts.sort(key=lambda post: post.run_at)
        return posts

    def has_been_sent(self, run_at: datetime) -> bool:
        return run_at.isoformat() in self._sent_cache

    def mark_sent(self, run_at: datetime) -> None:
        iso = run_at.isoformat()
        if iso in self._sent_cache:
            return
        with self.sent_log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{iso}\n")
        self._sent_cache.add(iso)

    def get_due_posts(self, now: datetime) -> list[ScheduledPost]:
        due: list[ScheduledPost] = []
        for post in self.load_posts():
            if self.has_been_sent(post.run_at):
                continue
            if post.run_at <= now:
                due.append(post)
        return due

    def next_available_slot(self, start: date, end: date, tzinfo) -> Optional[datetime]:
        booked_dates = {post.run_at.date() for post in self.load_posts()}
        for offset in range((end - start).days + 1):
            candidate = start + timedelta(days=offset)
            if candidate in booked_dates:
                continue
            base = datetime.combine(candidate, datetime.min.time(), tzinfo=tzinfo)
            return base.replace(hour=self.publish_hour, minute=0)
        return None

    def get_post_for_date(self, target_date: date) -> Optional[ScheduledPost]:
        for post in self.load_posts():
            if post.run_at.date() == target_date:
                return post
        return None

    def schedule_post(self, post: ScheduledPost) -> None:
        posts = self.load_posts()
        posts.append(post)
        posts.sort(key=lambda x: x.run_at)
        with self.posts_file.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["datetime", "text", "message_id"])
            for item in posts:
                writer.writerow([item.run_at.isoformat(), item.text, str(item.message_id)])

    def all_slots_filled(self, start: date, end: date) -> bool:
        posts = self.load_posts()
        total_days = (end - start).days + 1
        return len({post.run_at.date() for post in posts}) >= total_days

__all__ = ["PostStorage", "ScheduledPost"]
