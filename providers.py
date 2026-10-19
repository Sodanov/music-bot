import asyncio
import logging
import re
import time
from dataclasses import dataclass
from itertools import zip_longest
from urllib.parse import quote

import aiohttp

from config import (
    JAMENDO_CLIENT_ID,
    MAX_UPLOAD_BYTES,
    OPENVERSE_CLIENT_ID,
    OPENVERSE_CLIENT_SECRET,
    SEARCH_LIMIT_PER_SOURCE,
)

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)
APP_NAME = "MusicSearchBot"  # имя приложения для Audius
MAX_RESULTS = 40             # всего результатов в выдаче


@dataclass
class Track:
    source: str          # jamendo | audius | archive | ccmixter | openverse
    id: str
    title: str
    artist: str
    duration: int = 0    # секунды (0 — неизвестно)
    url: str = ""        # прямая ссылка на аудио (для archive определяется лениво)
    link: str = ""       # страница трека (для кнопки «Открыть»)


def _first(value) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


async def _get_json(session: aiohttp.ClientSession, url: str, name: str, params=None, headers=None):
    try:
        async with session.get(url, params=params, headers=headers, timeout=TIMEOUT) as r:
            r.raise_for_status()
            return await r.json(content_type=None)
    except Exception:
        log.exception("%s request failed", name)
        return None


# ---------- полные треки ----------

async def search_jamendo(session: aiohttp.ClientSession, query: str) -> list[Track]:
    if not JAMENDO_CLIENT_ID:
        return []
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": SEARCH_LIMIT_PER_SOURCE,
        "search": query,
        "audioformat": "mp32",
        "order": "relevance",
    }
    data = await _get_json(session, "https://api.jamendo.com/v3.0/tracks/", "Jamendo", params)
    if not data:
        return []
    return [
        Track(
            source="jamendo",
            id=str(t["id"]),
            title=t.get("name", ""),
            artist=t.get("artist_name", ""),
            duration=int(t.get("duration") or 0),
            url=t.get("audio", ""),
            link=t.get("shareurl", ""),
        )
        for t in data.get("results", [])
        if t.get("audio")
    ]


async def search_archive(session: aiohttp.ClientSession, query: str) -> list[Track]:
    # убираем спецсимволы синтаксиса Lucene
    safe = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", query).strip()
    if not safe:
        return []
    params = [
        ("q", f"(title:({safe}) OR creator:({safe})) AND mediatype:audio"),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "creator"),
        ("sort[]", "downloads desc"),
        ("rows", str(SEARCH_LIMIT_PER_SOURCE)),
        ("output", "json"),
    ]
    data = await _get_json(session, "https://archive.org/advancedsearch.php", "Archive", params)
    if not data:
        return []
    return [
        Track(
            source="archive",
            id=d["identifier"],
            title=_first(d.get("title")) or d["identifier"],
            artist=_first(d.get("creator")),
            link=f"https://archive.org/details/{d['identifier']}",
        )
        for d in data.get("response", {}).get("docs", [])
        if d.get("identifier")
    ]


async def search_audius(session: aiohttp.ClientSession, query: str) -> list[Track]:
    params = {"query": query, "limit": SEARCH_LIMIT_PER_SOURCE, "app_name": APP_NAME}
    data = await _get_json(session, "https://api.audius.co/v1/tracks/search", "Audius", params)
    if not data:
        return []
    out: list[Track] = []
    for t in data.get("data", []):
        tid = t.get("id")
        if not tid or t.get("is_streamable") is False or t.get("is_stream_gated"):
            continue
        permalink = t.get("permalink") or ""
        out.append(
            Track(
                source="audius",
                id=str(tid),
                title=t.get("title", ""),
                artist=(t.get("user") or {}).get("name", ""),
                duration=int(t.get("duration") or 0),
                url=f"https://api.audius.co/v1/tracks/{tid}/stream?app_name={APP_NAME}",
                link=f"https://audius.co{permalink}" if permalink else "",
            )
        )
    return out


async def search_ccmixter(session: aiohttp.ClientSession, query: str) -> list[Track]:
    params = {
        "f": "json",
        "search": query,
        "search_type": "all",
        "limit": SEARCH_LIMIT_PER_SOURCE,
        "sort": "rank",
    }
    data = await _get_json(session, "https://ccmixter.org/api/query", "ccMixter", params)
    if not isinstance(data, list):
        return []
    out: list[Track] = []
    for u in data:
        files = u.get("files") or []
        mp3 = next((f for f in files if str(f.get("download_url", "")).lower().endswith(".mp3")), None)
        if not mp3:
            continue
        out.append(
            Track(
                source="ccmixter",
                id=str(u.get("upload_id", "")),
                title=u.get("upload_name", ""),
                artist=u.get("user_name", ""),
                url=mp3["download_url"],
                link=u.get("file_page_url", ""),
            )
        )
    return out


_ov_token = {"value": "", "exp": 0.0}


async def _openverse_headers(session: aiohttp.ClientSession) -> dict:
    """С ключами Openverse лимиты намного выше; без них работаем анонимно."""
    if not (OPENVERSE_CLIENT_ID and OPENVERSE_CLIENT_SECRET):
        return {}
    now = time.time()
    if _ov_token["value"] and now < _ov_token["exp"]:
        return {"Authorization": f"Bearer {_ov_token['value']}"}
    try:
        async with session.post(
            "https://api.openverse.org/v1/auth_tokens/token/",
            data={
                "grant_type": "client_credentials",
                "client_id": OPENVERSE_CLIENT_ID,
                "client_secret": OPENVERSE_CLIENT_SECRET,
            },
            timeout=TIMEOUT,
        ) as r:
            r.raise_for_status()
            d = await r.json(content_type=None)
        _ov_token["value"] = d["access_token"]
        _ov_token["exp"] = now + int(d.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {_ov_token['value']}"}
    except Exception:
        log.exception("Openverse auth failed")
        return {}


async def search_openverse(session: aiohttp.ClientSession, query: str) -> list[Track]:
    params = {"q": query, "page_size": SEARCH_LIMIT_PER_SOURCE, "category": "music"}
    headers = await _openverse_headers(session)
    data = await _get_json(session, "https://api.openverse.org/v1/audio/", "Openverse", params, headers)
    if not isinstance(data, dict):
        return []
    out: list[Track] = []
    for t in data.get("results", []):
        url = t.get("url") or ""
        is_mp3 = str(t.get("filetype") or "").lower() == "mp3" or url.lower().split("?")[0].endswith(".mp3")
        if not url or not is_mp3:  # ogg/wav Telegram присылает файлом, а не аудио
            continue
        out.append(
            Track(
                source="openverse",
                id=str(t.get("id", "")),
                title=t.get("title") or "",
                artist=t.get("creator") or "",
                duration=int((t.get("duration") or 0) / 1000),  # в API — миллисекунды
                url=url,
                link=t.get("foreign_landing_url") or "",
            )
        )
    return out


# ---------- объединение ----------

def _key(t: Track) -> str:
    return re.sub(r"[\W_]+", "", f"{t.artist}{t.title}".lower())


def merge(lists: list[list[Track]]) -> list[Track]:
    """Чередуем источники и не повторяем одинаковые треки из разных источников."""
    seen: set[str] = set()
    mixed: list[Track] = []
    for group in zip_longest(*lists):
        for t in group:
            if t is None:
                continue
            k = _key(t)
            if k and k in seen:
                continue
            seen.add(k)
            mixed.append(t)
    return mixed[:MAX_RESULTS]


async def search_all(session: aiohttp.ClientSession, query: str) -> list[Track]:
    results = await asyncio.gather(
        search_audius(session, query),
        search_jamendo(session, query),
        search_archive(session, query),
        search_ccmixter(session, query),
        search_openverse(session, query),
        return_exceptions=True,
    )
    lists = [r for r in results if isinstance(r, list)]
    return merge(lists)


# ---------- получение файла ----------

async def resolve_url(session: aiohttp.ClientSession, track: Track) -> str | None:
    """Для archive.org находим первый подходящий mp3 внутри элемента."""
    if track.url:
        return track.url
    if track.source != "archive":
        return None
    data = await _get_json(session, f"https://archive.org/metadata/{track.id}", "Archive metadata")
    if not data:
        return None
    files = [
        f for f in data.get("files", [])
        if f.get("name", "").lower().endswith(".mp3")
        and 0 < int(f.get("size") or 0) <= MAX_UPLOAD_BYTES
    ]
    if not files:
        return None
    files.sort(key=lambda f: f.get("format") not in ("VBR MP3", "MP3"))
    track.url = f"https://archive.org/download/{track.id}/{quote(files[0]['name'], safe='/')}"
    return track.url


async def download(session: aiohttp.ClientSession, url: str) -> bytes | None:
    """Скачиваем файл сами, если Telegram не смог забрать его по ссылке."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
            r.raise_for_status()
            buf = bytearray()
            async for chunk in r.content.iter_chunked(64 * 1024):
                buf += chunk
                if len(buf) > MAX_UPLOAD_BYTES:
                    return None
            return bytes(buf)
    except Exception:
        log.exception("Download failed")
        return None
