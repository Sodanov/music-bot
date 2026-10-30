"""
Одноразовый перенос данных из локального bot.db (SQLite) в PostgreSQL/Neon.

Запуск:
  pip install asyncpg
  # macOS/Linux:
  export DATABASE_URL='строка подключения Neon'
  python migrate_sqlite_to_postgres.py
  # Windows PowerShell:
  $env:DATABASE_URL='строка подключения Neon'
  python migrate_sqlite_to_postgres.py

Положите этот файл рядом с bot.db. Скрипт ничего не удаляет из SQLite.
"""
import asyncio
import os
import sqlite3
import sys

import asyncpg

SQLITE_PATH = os.getenv("SQLITE_PATH", "bot.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

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
"""


def sqlite_rows(conn, sql):
    return conn.execute(sql).fetchall()


async def main():
    if not DATABASE_URL:
        sys.exit("Ошибка: сначала задайте переменную окружения DATABASE_URL.")
    if not os.path.isfile(SQLITE_PATH):
        sys.exit(
            f"Не найден файл {SQLITE_PATH!r}. Положите скрипт рядом с bot.db "
            "или задайте SQLITE_PATH."
        )

    source = sqlite3.connect(SQLITE_PATH)
    source.row_factory = sqlite3.Row

    # Проверяем, что это ожидаемая база и что в ней есть необходимые таблицы.
    tables = {
        row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {"managers", "ad", "users", "events"}
    missing = required - tables
    if missing:
        source.close()
        sys.exit("В bot.db отсутствуют таблицы: " + ", ".join(sorted(missing)))

    managers = [dict(r) for r in sqlite_rows(source, "SELECT user_id FROM managers")]
    ads = [dict(r) for r in sqlite_rows(
        source, "SELECT id, text, button_text, url, enabled FROM ad"
    )]
    users = [dict(r) for r in sqlite_rows(
        source, "SELECT user_id, username, first_name, first_seen, last_seen FROM users"
    )]
    events = [dict(r) for r in sqlite_rows(
        source, "SELECT id, ts, user_id, kind, source, query FROM events ORDER BY id"
    )]
    source.close()

    print(
        f"Найдено в SQLite: модераторов {len(managers)}, "
        f"строк рекламы {len(ads)}, пользователей {len(users)}, событий {len(events)}."
    )
    print("Перенос добавит/обновит эти записи в PostgreSQL; SQLite останется нетронутой.")

    conn = await asyncpg.connect(DATABASE_URL, command_timeout=60)
    try:
        async with conn.transaction():
            await conn.execute(SCHEMA)

            for row in managers:
                await conn.execute(
                    "INSERT INTO managers (user_id) VALUES ($1) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    row["user_id"],
                )

            for row in ads:
                await conn.execute(
                    """
                    INSERT INTO ad (id, text, button_text, url, enabled)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (id) DO UPDATE SET
                        text = EXCLUDED.text,
                        button_text = EXCLUDED.button_text,
                        url = EXCLUDED.url,
                        enabled = EXCLUDED.enabled
                    """,
                    row["id"], row["text"], row["button_text"], row["url"], row["enabled"],
                )

            for row in users:
                await conn.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        first_seen = EXCLUDED.first_seen,
                        last_seen = EXCLUDED.last_seen
                    """,
                    row["user_id"], row["username"], row["first_name"],
                    row["first_seen"], row["last_seen"],
                )

            for row in events:
                await conn.execute(
                    """
                    INSERT INTO events (id, ts, user_id, kind, source, query)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    row["id"], row["ts"], row["user_id"], row["kind"],
                    row["source"], row["query"],
                )

            # Восстанавливаем следующий ID после импорта явных id событий.
            await conn.execute("""
                SELECT setval(
                    pg_get_serial_sequence('events', 'id'),
                    GREATEST(COALESCE((SELECT MAX(id) FROM events), 1), 1),
                    EXISTS (SELECT 1 FROM events)
                )
            """)

        counts = await conn.fetchrow("""
            SELECT
              (SELECT COUNT(*) FROM managers) AS managers,
              (SELECT COUNT(*) FROM ad) AS ads,
              (SELECT COUNT(*) FROM users) AS users,
              (SELECT COUNT(*) FROM events) AS events
        """)
        print(
            "Готово. Сейчас в PostgreSQL: "
            f"модераторов {counts['managers']}, рекламных записей {counts['ads']}, "
            f"пользователей {counts['users']}, событий {counts['events']}."
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
