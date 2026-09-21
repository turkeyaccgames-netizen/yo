"""Downloads a clip so the bot can hand over the actual file, not just a link."""
import shutil
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
    ffmpeg = _ffmpeg_location()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"  دانلود ویدیو نشد ({url}): {str(e)[:120]}")
        return None
    files = [f for f in tmp.iterdir() if f.is_file() and f.stat().st_size > 0]
    if not files:
        return None
    clip = max(files, key=lambda f: f.stat().st_size)
    if clip.stat().st_size > max_mb * 1024 * 1024:
        print(f"  ویدیو بزرگ‌تر از {max_mb} مگابایت بود: {url}")
        return None
    return clip
