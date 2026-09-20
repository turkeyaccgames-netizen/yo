from urllib.parse import quote

from .common import RETRY_STATUSES, Post, SourceError, http_get

# FxTwitter public API (https://api.fxtwitter.com) — free, no login, 1000 req/min per IP.
API = "https://api.fxtwitter.com/2"


def fetch_account(handle: str) -> list[Post]:
    # FxTwitter answers 404 now and then for accounts that do exist, so 404 is retried here.
    data = http_get(f"{API}/profile/{handle}/statuses", params={"count": 20},
                    retries=3, retry_statuses=RETRY_STATUSES + (404,)).json()
    if data.get("code") != 200:
        raise SourceError(f"fxtwitter code {data.get('code')} for {handle}")
    posts = []
    for s in data.get("results") or []:
        if s.get("reposted_by") or s.get("replying_to"):
            continue
        media = s.get("media") or {}
        thumb = None
        if media.get("photos"):
            thumb = media["photos"][0].get("url")
        elif media.get("videos"):
            thumb = media["videos"][0].get("thumbnail_url")
        posts.append(Post(
            platform="x",
            id=s["id"],
            author=(s.get("author") or {}).get("screen_name") or handle,
            url=s.get("url") or f"https://x.com/{handle}/status/{s['id']}",
            text=s.get("text") or "",
            thumbnail=thumb,
            views=s.get("views"),
            likes=s.get("likes"),
            timestamp=s.get("created_timestamp"),
            is_video=bool(media.get("videos")),
        ))
    return posts


def fetch_trend_topics(count: int) -> list[tuple[str, str, str]]:
    """Returns (name, context, search_url) for current trending topics."""
    data = http_get(f"{API}/trends", params={"count": count}).json()
    return [
        (t["name"], t.get("context") or "", f"https://x.com/search?q={quote(t['name'])}")
        for t in (data.get("trends") or [])[:count]
    ]
