import re
import threading
import time
from dataclasses import dataclass

from curl_cffi import requests

PLATFORM_LABELS = {
    "youtube": "▶️ YouTube",
    "instagram": "📸 Instagram",
    "tiktok": "🎵 TikTok",
    "x": "𝕏 X",
}


@dataclass
class Post:
    platform: str
    id: str
    author: str
    url: str
    text: str = ""
    thumbnail: str | None = None
    views: int | None = None
    likes: int | None = None
    timestamp: int | None = None
    is_video: bool = False

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    @property
    def popularity(self) -> int:
        return self.views if self.views is not None else (self.likes or 0)


class SourceError(Exception):
    pass


_local = threading.local()


def http_get(url: str, *, params=None, headers=None, timeout=30, retries=2):
    if not hasattr(_local, "session"):
        _local.session = requests.Session(impersonate="chrome")
    last = None
    for attempt in range(retries + 1):
        try:
            r = _local.session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            last = SourceError(f"HTTP {r.status_code} for {url}")
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # network errors
            last = SourceError(f"{type(e).__name__}: {e}")
        time.sleep(2 * (attempt + 1))
    raise last


def parse_count(text: str | None) -> int | None:
    """'57k' -> 57000, '1.2M' -> 1200000, '871' -> 871."""
    if not text:
        return None
    m = re.match(r"\s*([\d.,]+)\s*([kKmMbB]?)", text)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2).lower(), 1)
    return int(num * mult)


_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}


def parse_relative_time(text: str | None, now: float | None = None) -> int | None:
    """'2 hours ago' / 'a day ago' -> unix timestamp (approximate)."""
    if not text:
        return None
    m = re.search(r"(\d+|an?)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text.lower())
    if not m:
        return None
    n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
    return int((now or time.time()) - n * _UNITS[m.group(2)])


def human(n: int | None) -> str:
    if n is None:
        return "?"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)
