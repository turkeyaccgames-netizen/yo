"""Public Telegram channels, read from the t.me/s/<channel> web preview: no login, no bot membership."""
import html
import re
from datetime import datetime

from .common import Post, SourceError, http_get, parse_count

_TAG_RE = re.compile(r"<[^>]+>")
_BG_RE = r"background-image:url\('([^']+)'\)"


def _text(block: str) -> str:
    m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
    if not m:
        return ""
    raw = re.sub(r"<br\s*/?>", "\n", m.group(1))
    return html.unescape(_TAG_RE.sub("", raw)).strip()


def fetch_channel(channel: str) -> list[Post]:
    channel = channel.lstrip("@")
    page = http_get(f"https://t.me/s/{channel}").text
    posts = []
    for block in page.split("tgme_widget_message_wrap")[1:]:
        ref = re.search(r'data-post="([^"/]+)/(\d+)"', block)
        if not ref:
            continue
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        views = re.search(r'tgme_widget_message_views">([^<]+)<', block)
        is_video = "tgme_widget_message_video_player" in block
        thumb = (re.search(r"tgme_widget_message_video_thumb\"[^>]*" + _BG_RE, block)
                 or re.search(r"tgme_widget_message_photo_wrap[^>]*" + _BG_RE, block))
        text = _text(block)
        # Channels sign every post with their own handle; it says nothing about the post.
        text = re.sub(rf"\s*@{re.escape(channel)}\b.*$", "", text, flags=re.I | re.S).strip() or text
        posts.append(Post(
            platform="telegram",
            id=f"{ref.group(1)}/{ref.group(2)}",
            author=ref.group(1),
            url=f"https://t.me/{ref.group(1)}/{ref.group(2)}",
            text=text,
            thumbnail=thumb.group(1) if thumb else None,
            views=parse_count(views.group(1)) if views else None,
            timestamp=int(datetime.fromisoformat(when.group(1)).timestamp()) if when else None,
            is_video=is_video,
        ))
    if not posts:
        # A private or mistyped channel answers with the plain "open in Telegram" page.
        raise SourceError(f"t.me/s/{channel} has no public posts (private channel or wrong name?)")
    return posts
