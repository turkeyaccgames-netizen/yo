import re
import threading
import time
import urllib.request
from dataclasses import dataclass

from curl_cffi import requests

PLATFORM_LABELS = {
    "youtube": "▶️ YouTube",
    "instagram": "📸 Instagram",
    "tiktok": "🎵 TikTok",
    "x": "𝕏 X",
    "telegram": "✈️ Telegram",
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
    # How many times this account's usual numbers the post reached; filled in by scoring.annotate.
    outlier: float | None = None

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    @property
    def popularity(self) -> int:
        return self.views if self.views is not None else (self.likes or 0)

    @property
    def heat(self) -> float:
        """Popularity weighted by how unusual it is for this account, so a small page's hit can outrank
        a huge page's ordinary day."""
        return self.popularity * min(max(self.outlier or 1.0, 0.5), 8.0)


class SourceError(Exception):
    pass


class BlockedError(SourceError):
    """The site answered, but refuses this IP (a Cloudflare challenge or a login wall). Retrying will not help."""


_local = threading.local()


RETRY_STATUSES = (429, 500, 502, 503, 504)
_CURL_NOISE = re.compile(r"Failed to perform, curl: \(\d+\) |\. See https://curl\.se/\S+ first for more details\.?")


def system_proxies() -> dict:
    """The proxy Windows is set to use (v2rayN, NekoBox, Hiddify in "system proxy" mode), or HTTP(S)_PROXY.

    yt-dlp already follows it, curl_cffi does not: without this, TikTok went through the VPN while YouTube,
    X, the Instagram mirrors and Telegram went out directly and were reset by the filter.
    """
    proxies = urllib.request.getproxies()
    return {k: v for k, v in proxies.items() if k in ("http", "https")}


def new_session() -> requests.Session:
    return requests.Session(impersonate="chrome", proxies=system_proxies() or None)


def short_error(e: Exception) -> str:
    return f"{type(e).__name__}: {_CURL_NOISE.sub('', str(e))}"


def http_get(url: str, *, params=None, headers=None, cookies=None, timeout=30, retries=2, retry_statuses=RETRY_STATUSES):
    if not hasattr(_local, "session"):
        _local.session = new_session()
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(2 * attempt)
        try:
            r = _local.session.get(url, params=params, headers=headers, cookies=cookies, timeout=timeout)
        except Exception as e:  # network errors
            last = SourceError(short_error(e))
            continue
        if r.status_code == 200:
            return r
        if r.status_code == 403 and r.headers.get("cf-mitigated") == "challenge":
            raise BlockedError(f"Cloudflare challenge for {url} (this IP is blocked; try another VPN server)")
        last = SourceError(f"HTTP {r.status_code} for {url}")
        if r.status_code not in retry_statuses:
            break
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
