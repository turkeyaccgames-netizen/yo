import time

from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget

from .common import Post, SourceError, http_get

_YDL_OPTS = {
    "extract_flat": True,
    "playlistend": 10,
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "impersonate": ImpersonateTarget.from_str("chrome"),
}


def _thumb(entry: dict) -> str | None:
    for t in entry.get("thumbnails") or []:
        if t.get("id") == "cover":
            return t.get("url")
    return (entry.get("thumbnails") or [{}])[0].get("url")


def fetch_account(username: str) -> list[Post]:
    try:
        with YoutubeDL(_YDL_OPTS) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{username}", download=False)
    except Exception as e:
        raise SourceError(f"yt-dlp: {str(e)[:200]}")
    posts = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        posts.append(Post(
            platform="tiktok",
            id=str(e["id"]),
            author=e.get("uploader") or username,
            url=e.get("url") or f"https://www.tiktok.com/@{username}/video/{e['id']}",
            text=e.get("description") or e.get("title") or "",
            thumbnail=_thumb(e),
            views=e.get("view_count"),
            likes=e.get("like_count"),
            timestamp=e.get("timestamp"),
            is_video=True,
        ))
    return posts


def fetch_trending(region: str, pages: int = 3, max_age_days: int = 30) -> list[Post]:
    """Popular For-You feed videos via the free tikwm.com API (max ~1 request/second)."""
    posts: dict[str, Post] = {}
    cutoff = time.time() - max_age_days * 86400
    for i in range(pages):
        if i:
            time.sleep(1.5)
        data = http_get("https://www.tikwm.com/api/feed/list", params={"region": region, "count": 20}).json()
        if data.get("code") != 0:
            raise SourceError(f"tikwm: {data.get('msg')}")
        for v in data.get("data") or []:
            if (v.get("create_time") or 0) < cutoff:
                continue
            author = (v.get("author") or {}).get("unique_id") or "unknown"
            posts[v["video_id"]] = Post(
                platform="tiktok",
                id=str(v["video_id"]),
                author=author,
                url=f"https://www.tiktok.com/@{author}/video/{v['video_id']}",
                text=v.get("title") or "",
                thumbnail=v.get("cover"),
                views=v.get("play_count"),
                likes=v.get("digg_count"),
                timestamp=v.get("create_time"),
                is_video=True,
            )
    return list(posts.values())
