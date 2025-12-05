from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class InitialPost:
    message_id: int
    text: str


class InitialPostStorage:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        if not self.file_path.exists():
            self.file_path.write_text("{}", encoding="utf-8")

    def save(self, message_id: int, text: str) -> None:
        payload = {"message_id": message_id, "text": text}
        self.file_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load(self) -> Optional[InitialPost]:
        if not self.file_path.exists():
            return None
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        message_id = data.get("message_id")
        if message_id is None:
            return None
        return InitialPost(message_id=int(message_id), text=data.get("text", ""))


__all__ = ["InitialPost", "InitialPostStorage"]
