"""Downloads a clip so the bot can hand over the actual file, not just a link."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget


def _ffmpeg_location() -> str | None:
    """Prefer a system ffmpeg (GitHub runners have one); fall back to the pip-installed binary."""
    if shutil.which("ffmpeg"):
        return None
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def has_video(path: Path) -> bool:
    """True unless ffmpeg can read the file and finds no picture in it (an unreadable check counts as yes)."""
    from .render import ffmpeg_exe

    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return True
    try:
        probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, timeout=30,
                               stdin=subprocess.DEVNULL)
    except Exception:
        return True
    info = probe.stderr.decode("utf-8", "replace")
    return "Stream #" not in info or "Video:" in info


def download_video(url: str, max_mb: int = 45) -> Path | None:
    """Returns a local mp4 small enough for Telegram, or None when the site will not hand it over."""
    tmp = Path(tempfile.mkdtemp(prefix="memebot-"))
    opts = {
        "outtmpl": str(tmp / "clip.%(ext)s"),
        # A single mp4 when the site has one; YouTube only serves split streams, which need ffmpeg.
        "format": f"best[ext=mp4][filesize_approx<{max_mb}M]/best[ext=mp4]/bv*[height<=1080][ext=mp4]+ba[ext=m4a]/best",
        "merge_output_format": "mp4",
        "max_filesize": max_mb * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "retries": 2,
        "impersonate": ImpersonateTarget.from_str("chrome"),
    }
    session = os.environ.get("INSTAGRAM_SESSIONID", "").strip()
    if session and "instagram.com" in url:
        # Instagram hands a reel to a logged-in visitor far more willingly.
        opts["http_headers"] = {"Cookie": f"sessionid={session}"}
    ffmpeg = _ffmpeg_location()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"  دانلود ویدیو نشد ({url}): {str(e)[:120]}")
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    files = [f for f in tmp.iterdir() if f.is_file() and f.stat().st_size > 0]
    clip = max(files, key=lambda f: f.stat().st_size) if files else None
    if clip and clip.stat().st_size > max_mb * 1024 * 1024:
        print(f"  ویدیو بزرگ‌تر از {max_mb} مگابایت بود: {url}")
        clip = None
    if clip and not has_video(clip):
        # A TikTok photo slideshow downloads as its soundtrack only; better the cover and the link.
        print(f"  فایل دانلودشده تصویر نداشت (احتمالاً اسلایدشو): {url}")
        clip = None
    if clip is None:
        shutil.rmtree(tmp, ignore_errors=True)
    return clip
