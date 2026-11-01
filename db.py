import logging
import os
import time

import asyncpg

log = logging.getLogger(__name__)
STORE_QUERIES = True
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS managers (user_id BIGINT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS ad (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    text TEXT NOT NULL DEFAULT '',
    button_text TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0
);
INSERT INTO ad (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    first_seen BIGINT NOT NULL,
    last_seen BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    ts BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT,
    query TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events (kind, ts);
CREATE INDEX IF NOT EXISTS idx_users_first_seen ON users (first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users (last_seen DESC);
"""

AD_FIELDS = {"text", "button_text", "url", "enabled"}


async def init() -> None:
    global _pool
    if _pool is not None:
        return
    if not DATABASE_URL:
        raise RuntimeError(
            "Не задана DATABASE_URL. Добавьте строку подключения PostgreSQL "
            "в Environment на Render."
        )
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL, min_size=1, max_size=5, command_timeout=30
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
    log.info("PostgreSQL подключён, схема базы проверена")


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _pool_or_raise() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("База не инициализирована: сначала вызовите db.init()")
    return _pool


async def is_manager(user_id: int) -> bool:
    async with _pool_or_raise().acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 FROM managers WHERE user_id = $1", user_id
        ) is not None


async def add_manager(user_id: int) -> None:
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(
            "INSERT INTO managers (user_id) VALUES ($1) "
            "ON CONFLICT (user_id) DO NOTHING", user_id
        )


async def remove_manager(user_id: int) -> bool:
    async with _pool_or_raise().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM managers WHERE user_id = $1", user_id
        )
        return result.endswith("1")


async def list_managers() -> list[int]:
    async with _pool_or_raise().acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM managers ORDER BY user_id")
        return [row["user_id"] for row in rows]


async def get_ad() -> dict:
    async with _pool_or_raise().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT text, button_text, url, enabled FROM ad WHERE id = 1"
        )
        if row is None:
            await conn.execute(
                "INSERT INTO ad (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
            )
            row = await conn.fetchrow(
                "SELECT text, button_text, url, enabled FROM ad WHERE id = 1"
            )
        return dict(row)


async def set_ad_field(field: str, value) -> None:
    if field not in AD_FIELDS:
        raise ValueError(f"unknown ad field: {field}")
    async with _pool_or_raise().acquire() as conn:
        await conn.execute(f"UPDATE ad SET {field} = $1 WHERE id = 1", value)


async def touch_user(
    user_id: int, username: str | None, first_name: str | None
) -> None:
    now = int(time.time())
    try:
        async with _pool_or_raise().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_seen = EXCLUDED.last_seen
                """,
                user_id, username, first_name, now, now,
            )
    except Exception:
        log.exception("stats: не удалось обновить пользователя %s", user_id)


async def log_event(
    user_id: int, kind: str, source: str | None = None,
    query: str | None = None
) -> None:
    if query is not None:
        query = query.strip().lower()[:100] if STORE_QUERIES else None
    try:
        async with _pool_or_raise().acquire() as conn:
            await conn.execute(
                "INSERT INTO events (ts, user_id, kind, source, query) "
                "VALUES ($1, $2, $3, $4, $5)",
                int(time.time()), user_id, kind, source, query,
            )
    except Exception:
        log.exception("stats: не удалось записать событие %s", kind)



async def count_users(search: str | None = None) -> int:
    """Количество пользователей, при необходимости — по username или Telegram ID."""
    async with _pool_or_raise().acquire() as conn:
        if not search:
            return await conn.fetchval("SELECT COUNT(*) FROM users")
        term = search.strip().lstrip("@")
        if term.isdigit():
            return await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE user_id = $1", int(term)
            )
        return await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE username ILIKE $1",
            f"%{term}%",
        )


async def list_users(
    offset: int = 0, limit: int = 10, search: str | None = None
) -> list[dict]:
    """Пользователи для админского списка; загружается только одна страница."""
    offset = max(0, offset)
    limit = min(max(1, limit), 25)
    async with _pool_or_raise().acquire() as conn:
        if not search:
            rows = await conn.fetch(
                """
                SELECT user_id, username, first_name, first_seen, last_seen
                FROM users
                ORDER BY first_seen DESC, user_id DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
        else:
            term = search.strip().lstrip("@")
            if term.isdigit():
                rows = await conn.fetch(
                    """
                    SELECT user_id, username, first_name, first_seen, last_seen
                    FROM users WHERE user_id = $1
                    ORDER BY first_seen DESC LIMIT $2 OFFSET $3
                    """,
                    int(term), limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT user_id, username, first_name, first_seen, last_seen
                    FROM users WHERE username ILIKE $1
                    ORDER BY first_seen DESC, user_id DESC
                    LIMIT $2 OFFSET $3
                    """,
                    f"%{term}%", limit, offset,
                )
    return [dict(row) for row in rows]



async def get_stats() -> dict:
    now = int(time.time())
    day, week = now - 86400, now - 7 * 86400

    async with _pool_or_raise().acquire() as conn:
        async def scalar(sql: str, *params) -> int:
            value = await conn.fetchval(sql, *params)
            return value if value is not None else 0

        stats = {
            "users_total": await scalar("SELECT COUNT(*) FROM users"),
            "users_new_day": await scalar(
                "SELECT COUNT(*) FROM users WHERE first_seen >= $1", day
            ),
            "users_new_week": await scalar(
                "SELECT COUNT(*) FROM users WHERE first_seen >= $1", week
            ),
            "users_active_day": await scalar(
                "SELECT COUNT(*) FROM users WHERE last_seen >= $1", day
            ),
            "users_active_week": await scalar(
                "SELECT COUNT(*) FROM users WHERE last_seen >= $1", week
            ),
        }

        for kind in ("search", "download"):
            stats[f"{kind}_total"] = await scalar(
                "SELECT COUNT(*) FROM events WHERE kind = $1", kind
            )
            stats[f"{kind}_day"] = await scalar(
                "SELECT COUNT(*) FROM events WHERE kind = $1 AND ts >= $2",
                kind, day,
            )
            stats[f"{kind}_week"] = await scalar(
                "SELECT COUNT(*) FROM events WHERE kind = $1 AND ts >= $2",
                kind, week,
            )

        rows = await conn.fetch(
            "SELECT COALESCE(source, '?') AS source, COUNT(*) AS count "
            "FROM events WHERE kind = 'download' GROUP BY source"
        )
        stats["by_source"] = {row["source"]: row["count"] for row in rows}

        rows = await conn.fetch(
            """
            SELECT query, COUNT(*) AS c
            FROM events
            WHERE kind = 'search' AND query IS NOT NULL AND ts >= $1
            GROUP BY query
            ORDER BY c DESC, MAX(ts) DESC
            LIMIT 10
            """,
            week,
        )
        stats["top_queries"] = [(row["query"], row["c"]) for row in rows]

        # Кампании: считаем уникальных пользователей, пришедших по /start <метка>.
        rows = await conn.fetch(
            """
            SELECT source AS campaign, COUNT(DISTINCT user_id) AS users,
                   COUNT(*) AS starts, MAX(ts) AS last_seen
            FROM events
            WHERE kind = 'start' AND source IS NOT NULL AND source <> 'direct'
            GROUP BY source
            ORDER BY users DESC, starts DESC, last_seen DESC
            LIMIT 10
            """
        )
        stats["campaigns"] = [dict(row) for row in rows]
    return stats
