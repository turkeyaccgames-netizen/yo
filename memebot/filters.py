"""Keyword filters that keep the funny/cool posts and drop the news-y or grim ones."""
import re

from .common import Post

# Unmistakable "this is a joke" markers. With one of these, an ambiguous word ("dead", "killed it")
# is slang, not news.
LAUGH = ("😂", "🤣", "💀", "😭", "🤡", "😹", "lol", "lmao", "lmfao")


def _hits(text: str | None, keywords: list[str]) -> int:
    """Words match whole words only ('war' must not match 'warm', «فوت» must not match «فوتبال»);
    emoji and hashtags match anywhere."""
    low = (text or "").lower()
    if not low:
        return 0
    count = 0
    for word in keywords:
        word = word.lower()
        if word.replace(" ", "").replace("‌", "").isalpha():
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low):
                count += 1
        elif word in low:
            count += 1
    return count


def contains(text: str | None, keywords: list[str]) -> bool:
    return _hits(text, keywords) > 0


def is_serious(post: Post, cfg: dict) -> bool:
    if contains(post.text, cfg.get("blocked_keywords", [])):
        return True
    # "dead", "crash", "police" are news in a headline and slang in a joke.
    if contains(post.text, cfg.get("soft_blocked_keywords", [])):
        return not any(m in (post.text or "").lower() for m in LAUGH)
    return False


def is_funny(post: Post, cfg: dict) -> bool:
    """One clear marker, or two weak ones: "bro" or "🔥" alone says nothing."""
    if contains(post.text, cfg.get("funny_keywords", [])):
        return True
    return _hits(post.text, cfg.get("weak_funny_keywords", [])) >= 2


def drop_serious(posts: list[Post], cfg: dict) -> list[Post]:
    if not cfg.get("skip_serious", True):
        return posts
    return [p for p in posts if not is_serious(p, cfg)]
