import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .common import Post, SourceError, http_get

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
API = "https://www.googleapis.com/youtube/v3"


def resolve_channel_id(ref: str, cache: dict) -> str:
    """Accepts 'UC...' or '@handle'; handles are resolved once and cached in state."""
    if ref.startswith("UC"):
        return ref
    if ref in cache:
        return cache[ref]
    handle = ref.lstrip("@")
    r = http_get(f"https://www.youtube.com/@{handle}", headers={"Accept-Language": "en-US,en;q=0.9"})
    m = re.search(r'"externalId":"(UC[\w-]{22})"', r.text)
    if not m:
        raise SourceError(f"could not resolve YouTube handle {ref}")
    cache[ref] = m.group(1)
    return cache[ref]


def fetch_channel(channel_id: str) -> list[Post]:
    r = http_get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id})
    root = ET.fromstring(r.content)
    posts = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", namespaces=NS)
        link = entry.find("atom:link[@rel='alternate']", NS)
        stats = entry.find("media:group/media:community/media:statistics", NS)
        published = entry.findtext("atom:published", namespaces=NS)
        posts.append(Post(
            platform="youtube",
            id=vid,
            author=entry.findtext("atom:author/atom:name", namespaces=NS) or channel_id,
            url=link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}",
            text=entry.findtext("atom:title", namespaces=NS) or "",
            thumbnail=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            views=int(stats.get("views")) if stats is not None and stats.get("views") else None,
            timestamp=int(datetime.fromisoformat(published).timestamp()) if published else None,
            is_video=True,
        ))
    return posts


def fetch_trending_api(api_key: str, query: str, region: str, window_hours: int, limit: int = 25) -> list[Post]:
    """Most-viewed videos matching the query published in the window (YouTube Data API, ~101 quota units)."""
    after = datetime.fromtimestamp(time.time() - window_hours * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    search = http_get(f"{API}/search", params={
        "part": "id", "type": "video", "order": "viewCount", "q": query, "publishedAfter": after,
        "regionCode": region, "relevanceLanguage": "en", "maxResults": limit, "key": api_key,
    }).json()
    ids = [it["id"]["videoId"] for it in search.get("items", [])]
    if not ids:
        return []
    videos = http_get(f"{API}/videos", params={"part": "snippet,statistics", "id": ",".join(ids), "key": api_key}).json()
    posts = []
    for v in videos.get("items", []):
        sn, st = v["snippet"], v.get("statistics", {})
        posts.append(Post(
            platform="youtube",
            id=v["id"],
            author=sn.get("channelTitle", ""),
            url=f"https://www.youtube.com/watch?v={v['id']}",
            text=sn.get("title", ""),
            thumbnail=f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg",
            views=int(st["viewCount"]) if "viewCount" in st else None,
            likes=int(st["likeCount"]) if "likeCount" in st else None,
            timestamp=int(datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")).timestamp()),
            is_video=True,
        ))
    return posts
