"""Burns the caption onto the clip, so what arrives in Telegram is a finished meme, not homework.

The classic layout: a white band above the video with the line in bold black. The band is drawn with
Pillow (ffmpeg's own text filter cannot shape Persian) and laid over the clip with ffmpeg.
"""
import re
import shutil
import subprocess
from pathlib import Path

WIDTH = 720
MARGIN = 28
FONT_SIZE = 40
MAX_LINES = 5
FONTS = (
    "C:/Windows/Fonts/tahomabd.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
# These fonts have no emoji glyphs; an emoji would be drawn as an empty box.
_UNDRAWABLE = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0000fe00-\U0000fe0f\U0000200d]")
_RTL = re.compile("[\u0590-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")


def ffmpeg_exe() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _font():
    from PIL import ImageFont

    for path in FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, FONT_SIZE)
    return None


def _wrap(text: str, font, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _visual(line: str) -> str:
    """Persian has to be joined and reordered by hand: Pillow's basic layout draws letters left to right, unjoined."""
    if not _RTL.search(line):
        return line
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(line))


def caption_band(text: str, out: Path) -> int | None:
    """Writes the white caption band as a PNG and returns its height, or None when it cannot be drawn."""
    from PIL import Image, ImageDraw

    font = _font()
    text = " ".join(_UNDRAWABLE.sub("", text or "").split())
    if font is None or not text:
        return None
    lines = _wrap(text, font, WIDTH - 2 * MARGIN)
    if len(lines) > MAX_LINES:
        return None  # a wall of text is not a caption
    line_height = int(FONT_SIZE * 1.35)
    height = line_height * len(lines) + 2 * MARGIN
    height += height % 2  # x264 wants even dimensions
    image = Image.new("RGB", (WIDTH, height), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        shown = _visual(line)
        x = (WIDTH - font.getlength(shown)) / 2
        draw.text((x, MARGIN + i * line_height), shown, font=font, fill="black")
    image.save(out)
    return height


def make_meme(clip: Path, text: str, max_mb: int = 45, timeout: int = 240) -> Path | None:
    """Returns a new mp4 next to `clip` with the caption on top, or None (the caller then sends the plain clip)."""
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return None
    band = clip.with_name("caption.png")
    out = clip.with_name("meme.mp4")
    try:
        height = caption_band(text, band)
        if not height:
            return None
        graph = (f"[0:v]scale={WIDTH}:-2,pad={WIDTH}:ih+{height}:0:{height}:white[v];"
                 f"[v][1:v]overlay=0:0,format=yuv420p[out]")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(clip), "-i", str(band),
               "-filter_complex", graph, "-map", "[out]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", str(out)]
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if done.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            print(f"  ساخت میم نشد: {done.stderr.decode('utf-8', 'replace')[-200:]}")
            return None
        if out.stat().st_size > max_mb * 1024 * 1024:
            return None
        return out
    except Exception as e:  # a missing font, a codec ffmpeg cannot read, a timeout on a long clip
        print(f"  ساخت میم نشد: {str(e)[:150]}")
        return None
