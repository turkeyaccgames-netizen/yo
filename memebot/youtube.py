import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from yt_dlp import YoutubeDL

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
    # The cookies skip YouTube's cookie-consent interstitial, which otherwise hides the channel id.
    r = http_get(f"https://www.youtube.com/@{handle}", headers={"Accept-Language": "en-US,en;q=0.9"},
                 cookies={"SOCS": "CAI", "CONSENT": "YES+1"})
    m = re.search(r'"externalId":"(UC[\w-]{22})"', r.text) or re.search(r'channel/(UC[\w-]{22})', r.text)
    if not m:
        raise SourceError(f"could not resolve YouTube handle {ref}")
    cache[ref] = m.group(1)
    return cache[ref]


def fetch_channel(channel_id: str, shorts_only: bool = False) -> list[Post]:
    """The RSS feed first (it has dates); when YouTube has it down, which happens for hours at a time, yt-dlp."""
    try:
        return _from_feed(channel_id, shorts_only)
    except Exception as feed_error:
        try:
            return _from_channel_tab(channel_id, shorts_only)
        except Exception as e:
            raise SourceError(f"feed: {feed_error} | yt-dlp: {str(e)[:150]}")


def _from_channel_tab(channel_id: str, shorts_only: bool, limit: int = 5) -> list[Post]:
    """Only the newest few: this listing has no upload dates, so the age filter cannot catch an old video."""
    tab = "shorts" if shorts_only else "videos"
    opts = {"extract_flat": True, "playlistend": limit, "quiet": True, "no_warnings": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/channel/{channel_id}/{tab}", download=False)
    author = info.get("channel") or info.get("uploader") or channel_id
    posts = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        posts.append(Post(
            platform="youtube",
            id=e["id"],
            author=author,
            url=f"https://www.youtube.com/shorts/{e['id']}" if shorts_only else f"https://www.youtube.com/watch?v={e['id']}",
            text=e.get("title") or "",
            thumbnail=f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
            views=e.get("view_count"),
            is_video=True,
        ))
    if not posts:
        raise SourceError(f"no videos in the {tab} tab")
    return posts


def _from_feed(channel_id: str, shorts_only: bool) -> list[Post]:
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
    if shorts_only:
        # The feed links a Short as youtube.com/shorts/<id> and a normal video as /watch?v=<id>.
        posts = [p for p in posts if "/shorts/" in p.url]
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
