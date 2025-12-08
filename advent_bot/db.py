from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import psycopg
from psycopg import conninfo, sql
from psycopg.errors import InvalidCatalogName
from psycopg_pool import ConnectionPool


@dataclass
class User:
    user_id: int
    username: Optional[str]
    status: str


class UserRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ensure_database_exists()
        self._pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=5, timeout=10)
        self._initialize()

    def _ensure_database_exists(self) -> None:
        try:
            with psycopg.connect(self._dsn):
                return
        except psycopg.OperationalError as exc:
            sqlstate = getattr(exc, "sqlstate", getattr(exc, "pgcode", None))
            if sqlstate != InvalidCatalogName.sqlstate:
                raise
            self._create_database()

    def _create_database(self) -> None:
        info = conninfo.conninfo_to_dict(self._dsn)
        db_name = info.get("dbname")
        if not db_name:
            raise RuntimeError("DATABASE_URL must include database name")
        admin_info = {key: value for key, value in info.items() if value is not None}
        admin_info["dbname"] = "postgres"
        admin_dsn = conninfo.make_conninfo(**admin_info)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                exists = cur.fetchone()
                if exists:
                    return
                cur.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
                )

    def _initialize(self) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        status TEXT NOT NULL CHECK (status IN ('active', 'stop'))
                    )
                    """
                )
                conn.commit()

    async def upsert_user(self, user_id: int, username: Optional[str], status: str) -> None:
        await asyncio.to_thread(self._upsert_user, user_id, username, status)

    def _upsert_user(self, user_id: int, username: Optional[str], status: str) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (user_id, username, status)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        status = EXCLUDED.status
                    """,
                    (user_id, username, status),
                )
                conn.commit()

    async def get_user(self, user_id: int) -> Optional[User]:
        return await asyncio.to_thread(self._get_user, user_id)

    def _get_user(self, user_id: int) -> Optional[User]:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, username, status FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return User(user_id=row[0], username=row[1], status=row[2])

    async def get_active_user_ids(self) -> list[int]:
        return await asyncio.to_thread(self._get_active_user_ids)

    def _get_active_user_ids(self) -> list[int]:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE status = 'active'")
                return [row[0] for row in cur.fetchall()]


__all__ = ["UserRepository", "User"]
