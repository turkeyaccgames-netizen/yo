"""Optional: lets Claude pick which caption goes on which clip, instead of the keyword rules.

The keyword matcher never sees the video. Here the model gets each clip's cover image and
description next to the candidate lines, pairs them (or writes a better line itself), and gives the
caption in the owner's language. Without ANTHROPIC_API_KEY this module does nothing and the bot
falls back to combos.find_pairs.
"""
import base64
import json
import os

from .common import Post, http_get, human

DEFAULT_MODEL = "claude-haiku-5-5"
LANGUAGES = {"fa": "Persian (Farsi)", "en": "English", "ar": "Arabic", "tr": "Turkish"}

SYSTEM = """You help the owner of a meme page turn trending short clips into ready-to-post memes.

A meme of this kind is a short clip with one line of text above it. The line reframes the clip: it \
names a relatable situation ("me when the wifi drops for 2 seconds") and the clip becomes the \
reaction or the punchline. It works only when the text and what is visibly happening in the clip \
fit each other; a funny line over an unrelated clip is not a meme.

You receive a numbered list of clips (platform, account, view count, the uploader's own \
description, and usually the cover frame) and a numbered list of candidate lines collected from \
popular humor accounts. You only see one frame per clip, so judge what happens from the frame and \
the description together, and prefer clips where you can actually tell what is going on.

For each meme you propose:
- Pick a clip, and either pick the candidate line that fits it best or write a better line \
yourself. Writing your own is welcome when no candidate truly fits; then leave caption_id empty.
- Give the line in English (caption_en) and in {language} (caption_local). The {language} version \
is what gets posted, so write it the way people in that language actually joke online: natural, \
colloquial, culturally at home. Do not translate word for word, and swap references that would not \
land for something that does. Keep it under 90 characters and free of hashtags.
- Score the pairing 1-10 for how well text and clip fit and how likely it is to make someone \
laugh. Be honest: 5 means forgettable. Low scores are filtered out, so a weak pairing costs \
nothing, while an inflated score puts a bad meme in front of the owner.

Use each clip and each candidate line at most once. Skip clips that are advertisements, news, \
politics, tragedy, or anything where the joke would be at the expense of someone's suffering. \
Propose at most {count} memes, best first; fewer is fine when the material is thin."""

SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clip_id": {"type": "string"},
                    "caption_id": {"type": "string"},
                    "caption_en": {"type": "string"},
                    "caption_local": {"type": "string"},
                    "score": {"type": "integer"},
                },
                "required": ["clip_id", "caption_id", "caption_en", "caption_local", "score"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["pairs"],
    "additionalProperties": False,
}

_MAGIC = ((b"\xff\xd8", "image/jpeg"), (b"\x89PNG", "image/png"), (b"GIF8", "image/gif"))


def enabled(cfg: dict) -> bool:
    return bool(cfg.get("enabled", True) and os.environ.get("ANTHROPIC_API_KEY"))


def _image_block(url: str | None) -> dict | None:
    if not url:
        return None
    try:
        blob = http_get(url, retries=0, timeout=15).content
    except Exception:
        return None
    media_type = next((kind for magic, kind in _MAGIC if blob.startswith(magic)), None)
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        media_type = "image/webp"
    if not media_type or len(blob) > 3_000_000:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.standard_b64encode(blob).decode()}}


def build_content(lines: list[Post], clips: list[Post], max_images: int) -> list[dict]:
    content = [{"type": "text", "text": "CLIPS"}]
    for i, clip in enumerate(clips):
        unusual = f", {clip.outlier:g}x this account's usual" if clip.outlier and clip.outlier >= 1.5 else ""
        content.append({"type": "text", "text": (
            f"clip c{i} | {clip.platform} @{clip.author} | {human(clip.popularity)} views{unusual}\n"
            f"description: {' '.join((clip.text or '(none)').split())[:400]}")})
        image = _image_block(clip.thumbnail) if i < max_images else None
        if image:
            content.append(image)
    listing = "\n".join(f"t{i}: {' '.join(p.text.split())[:240]}" for i, p in enumerate(lines))
    content.append({"type": "text", "text": f"CANDIDATE LINES\n{listing or '(none this time; write your own)'}"})
    return content


def parse_pairs(raw: str, lines: list[Post], clips: list[Post], min_score: int) -> list[dict]:
    """Turns the model's answer back into Post pairs, dropping anything that points at nothing."""
    pairs, used_clips, used_lines = [], set(), set()
    for item in json.loads(raw).get("pairs", []):
        clip_ref, line_ref = item["clip_id"].strip().lower(), item["caption_id"].strip().lower()
        if not (clip_ref[:1] == "c" and clip_ref[1:].isdigit()) or int(clip_ref[1:]) >= len(clips):
            continue
        clip = clips[int(clip_ref[1:])]
        line = None
        if line_ref[:1] == "t" and line_ref[1:].isdigit() and int(line_ref[1:]) < len(lines):
            line = lines[int(line_ref[1:])]
        if item["score"] < min_score or clip.key in used_clips or (line and line.key in used_lines):
            continue
        text = " ".join(item["caption_en"].split())
        local = " ".join(item["caption_local"].split())
        if not text and not local:
            continue
        used_clips.add(clip.key)
        if line:
            used_lines.add(line.key)
        pairs.append({"video": clip, "tweet": line, "text": text or local, "local": local or text,
                      "score": float(item["score"]), "shared": [], "by_ai": True})
    return pairs


def make_pairs(lines: list[Post], clips: list[Post], count: int, cfg: dict) -> list[dict] | None:
    """Returns pairs in the shape send_combos expects, or None when the model could not be asked."""
    import anthropic

    language = LANGUAGES.get(cfg.get("caption_language", "fa"), cfg.get("caption_language", "fa"))
    clips = clips[: cfg.get("max_clips", 14)]
    lines = lines[: cfg.get("max_lines", 30)]
    if not clips:
        return []
    client = anthropic.Anthropic(timeout=180.0, max_retries=1)
    try:
        response = client.messages.create(
            model=cfg.get("model", DEFAULT_MODEL),
            max_tokens=16000,
            system=SYSTEM.replace("{language}", language).replace("{count}", str(count)),
            messages=[{"role": "user", "content": build_content(lines, clips, cfg.get("max_images", 14))}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.AuthenticationError:
        print("[ai] کلید ANTHROPIC_API_KEY درست نیست؛ ترکیب‌ها با روش کلمه‌ای ساخته می‌شوند.")
        return None
    except anthropic.PermissionDeniedError as e:
        # What Anthropic answers to an IP in an unsupported region.
        print(f"[ai] دسترسی رد شد (VPN روشن است؟): {e.message[:150]}")
        return None
    except anthropic.RateLimitError:
        print("[ai] سقف درخواست پر شده؛ این نوبت با روش کلمه‌ای ساخته می‌شود.")
        return None
    except anthropic.APIStatusError as e:
        print(f"[ai] خطای API ({e.status_code}): {e.message[:200]}")
        return None
    except anthropic.APIConnectionError as e:
        print(f"[ai] ارتباط با Anthropic برقرار نشد: {str(e)[:150]}")
        return None
    if response.stop_reason != "end_turn":
        print(f"[ai] پاسخ کامل نبود ({response.stop_reason})؛ این نوبت با روش کلمه‌ای ساخته می‌شود.")
        return None
    raw = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return parse_pairs(raw, lines, clips, cfg.get("min_score", 6))
    except (ValueError, KeyError, TypeError) as e:
        print(f"[ai] پاسخ خوانده نشد: {e}")
        return None
