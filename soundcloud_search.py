import asyncio
import yt_dlp

def _search(query: str, limit: int) -> list[dict]:
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
    out = []
    for e in info.get("entries", []):
        if not e or not e.get("url"):
            continue
        out.append({
            "title": e.get("title") or "",
            "artist": e.get("uploader") or "",
            "duration_sec": int(e.get("duration") or 0),
            "url": e["url"],
        })
    return out

async def search(query: str, limit: int = 20) -> list[dict]:
    return await asyncio.to_thread(_search, query, limit)