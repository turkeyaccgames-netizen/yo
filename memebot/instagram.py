import html
import re

from .common import Post, SourceError, http_get, parse_count, parse_relative_time


def _from_imginn(username: str) -> list[Post]:
    """Public mirror that serves the latest ~12 posts without an Instagram login."""
    page = http_get(f"https://imginn.com/{username}/").text
    posts = []
    for block in page.split('<div class="item">')[1:]:
        code = re.search(r'href="/p/([\w-]+)/"', block)
        if not code:  # template placeholder like /p/{code}/
            continue
        img = re.search(r'<img[^>]*?\ssrc="([^"]+)"', block)
        alt = re.search(r'\salt="([^"]*)"', block)
        likes = re.search(r'class="likes">.*?<span>([^<]*)</span>', block, re.S)
        when = re.search(r'class="time">([^<]+)<', block)
        caption = html.unescape(alt.group(1)) if alt else ""
        caption = re.sub(rf"\s*by @{re.escape(username)}\s*$", "", caption, flags=re.I)
        posts.append(Post(
            platform="instagram",
            id=code.group(1),
            author=username,
            url=f"https://www.instagram.com/p/{code.group(1)}/",
            text=caption,
            thumbnail=html.unescape(img.group(1)) if img else None,
            likes=parse_count(likes.group(1)) if likes else None,
            timestamp=parse_relative_time(when.group(1)) if when else None,
            is_video="icon-video" in block,
        ))
    if not posts:
        raise SourceError(f"imginn returned no posts for {username}")
    return posts


def _from_instagram_api(username: str) -> list[Post]:
    """Instagram's own web endpoint; often rate-limited, used only as a fallback."""
    r = http_get(
        "https://i.instagram.com/api/v1/users/web_profile_info/",
        params={"username": username},
        headers={"x-ig-app-id": "936619743392459"},
        retries=0,
    )
    user = r.json()["data"]["user"]
    posts = []
    for edge in user["edge_owner_to_timeline_media"]["edges"]:
        n = edge["node"]
        caps = n.get("edge_media_to_caption", {}).get("edges", [])
        posts.append(Post(
            platform="instagram",
            id=n["shortcode"],
            author=username,
            url=f"https://www.instagram.com/p/{n['shortcode']}/",
            text=caps[0]["node"]["text"] if caps else "",
            thumbnail=n.get("display_url"),
            views=n.get("video_view_count"),
            likes=(n.get("edge_liked_by") or n.get("edge_media_preview_like") or {}).get("count"),
            timestamp=n.get("taken_at_timestamp"),
            is_video=bool(n.get("is_video")),
        ))
    return posts


def fetch_account(username: str) -> list[Post]:
    try:
        return _from_imginn(username)
    except Exception as first:
        try:
            return _from_instagram_api(username)
        except Exception as second:
            raise SourceError(f"imginn: {first} | instagram api: {second}")
