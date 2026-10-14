import html
import os
import re

from .common import BlockedError, Post, SourceError, http_get, parse_count, parse_relative_time


MIRRORS = ("https://imginn.com/{}/", "https://imgsed.com/{}/")


def _from_mirror(url_template: str, username: str) -> list[Post]:
    """Public mirrors that serve the latest ~12 posts without an Instagram login."""
    page = http_get(url_template.format(username)).text
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
        raise SourceError(f"{url_template.split('/')[2]} returned no posts for {username}")
    return posts


def session_cookie() -> dict | None:
    """The sessionid of a logged-in (spare) Instagram account, when the owner has put one in .env.

    Without it Instagram answers 401 from most IPs and the mirrors sit behind Cloudflare, which is
    why this source failed four runs out of five.
    """
    value = os.environ.get("INSTAGRAM_SESSIONID", "").strip()
    return {"sessionid": value} if value else None


def _from_instagram_api(username: str) -> list[Post]:
    """Instagram's own web endpoint. Reliable with a session cookie; rate-limited and often refused without."""
    r = http_get(
        "https://i.instagram.com/api/v1/users/web_profile_info/",
        params={"username": username},
        headers={"x-ig-app-id": "936619743392459"},
        cookies=session_cookie(),
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


_blocked: dict[str, str] = {}  # source -> reason, for the rest of this run


def fetch_account(username: str) -> list[Post]:
    """Tries each source in turn. A source that refused this IP once is not asked again for the other accounts."""
    sources = [(t.split("/")[2], lambda t=t: _from_mirror(t, username)) for t in MIRRORS]
    official = ("instagram.com", lambda: _from_instagram_api(username))
    # Logged in, Instagram itself is the dependable source; anonymous, it is the last resort.
    sources = [official] + sources if session_cookie() else sources + [official]
    problems = []
    for name, fetch in sources:
        if name in _blocked:
            problems.append(f"{name}: {_blocked[name]}")
            continue
        try:
            return fetch()
        except BlockedError as e:
            _blocked[name] = "blocked (Cloudflare challenge)"
            problems.append(f"{name}: {e}")
        except Exception as e:
            if name == "instagram.com" and "HTTP 401" in str(e):
                _blocked[name] = ("the INSTAGRAM_SESSIONID cookie has expired; log in again and copy a new one"
                                  if session_cookie() else "wants a login from this IP (HTTP 401)")
            problems.append(f"{name}: {e}")
    if len(_blocked) == len(sources):
        raise BlockedError("every Instagram source refuses this IP; switch the VPN server/location | " + " | ".join(problems))
    raise SourceError(" | ".join(problems))
