import logging
import time

import aiosqlite

from config import DB_PATH

log = logging.getLogger(__name__)

# Хранить ли тексты поисковых запросов (нужны для «топа запросов»).
# False — тексты не сохраняются, топ в статистике будет пустым.
STORE_QUERIES = True

SCHEMA = """
CREATE TABLE IF NOT EXISTS managers (
    user_id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS ad (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    text TEXT NOT NULL DEFAULT '',
    button_text TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO ad (id) VALUES (1);

-- статистика: время везде в секундах Unix (UTC)
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,   -- 'search' или 'download'
    source TEXT,          -- для скачиваний: 'youtube' или 'catalog'
    query TEXT            -- для поисков: текст запроса (если STORE_QUERIES)
);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events (kind, ts);
"""

AD_FIELDS = {"text", "button_text", "url", "enabled"}


async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def is_manager(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT 1 FROM managers WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None


async def add_manager(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("INSERT OR IGNORE INTO managers (user_id) VALUES (?)", (user_id,))
        await conn.commit()


async def remove_manager(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("DELETE FROM managers WHERE user_id = ?", (user_id,))
        await conn.commit()
        return cur.rowcount > 0


async def list_managers() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT user_id FROM managers ORDER BY user_id") as cur:
            return [row[0] for row in await cur.fetchall()]


async def get_ad() -> dict:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT text, button_text, url, enabled FROM ad WHERE id = 1") as cur:
            row = await cur.fetchone()
            return dict(row)


async def set_ad_field(field: str, value) -> None:
    if field not in AD_FIELDS:
        raise ValueError(f"unknown ad field: {field}")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(f"UPDATE ad SET {field} = ? WHERE id = 1", (value,))
        await conn.commit()


# ---------- статистика ----------
# Ошибки здесь только пишутся в лог: сбой статистики не должен ломать бота.

async def touch_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Записывает пользователя или обновляет его имя и время последней активности."""
    now = int(time.time())
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen = excluded.last_seen
                """,
                (user_id, username, first_name, now, now),
            )
            await conn.commit()
    except Exception:
        log.exception("stats: не удалось обновить пользователя %s", user_id)


async def log_event(user_id: int, kind: str, source: str | None = None, query: str | None = None) -> None:
    """Записывает событие: kind='search' (с query) или kind='download' (с source)."""
    if query is not None:
        # в нижнем регистре, чтобы «Believer» и «believer» считались одним запросом
        query = query.strip().lower()[:100] if STORE_QUERIES else None
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO events (ts, user_id, kind, source, query) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), user_id, kind, source, query),
            )
            await conn.commit()
    except Exception:
        log.exception("stats: не удалось записать событие %s", kind)


async def get_stats() -> dict:
    """Сводная статистика. «Сутки» и «неделя» — скользящие окна 24 часа и 7 дней."""
    now = int(time.time())
    day, week = now - 86400, now - 7 * 86400

    async with aiosqlite.connect(DB_PATH) as conn:

        async def scalar(sql: str, params: tuple = ()) -> int:
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

        stats = {
            "users_total": await scalar("SELECT COUNT(*) FROM users"),
            "users_new_day": await scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (day,)),
            "users_new_week": await scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (week,)),
            "users_active_day": await scalar("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (day,)),
            "users_active_week": await scalar("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (week,)),
        }

        for kind in ("search", "download"):
            stats[f"{kind}_total"] = await scalar("SELECT COUNT(*) FROM events WHERE kind = ?", (kind,))
            stats[f"{kind}_day"] = await scalar(
                "SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= ?", (kind, day)
            )
            stats[f"{kind}_week"] = await scalar(
                "SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= ?", (kind, week)
            )

        async with conn.execute(
            "SELECT COALESCE(source, '?'), COUNT(*) FROM events WHERE kind = 'download' GROUP BY source"
        ) as cur:
            stats["by_source"] = {row[0]: row[1] for row in await cur.fetchall()}

        async with conn.execute(
            """
            SELECT query, COUNT(*) AS c
            FROM events
            WHERE kind = 'search' AND query IS NOT NULL AND ts >= ?
            GROUP BY query
            ORDER BY c DESC, MAX(ts) DESC
            LIMIT 10
            """,
            (week,),
        ) as cur:
            stats["top_queries"] = [(row[0], row[1]) for row in await cur.fetchall()]

    return stats
