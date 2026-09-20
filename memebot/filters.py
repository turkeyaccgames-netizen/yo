"""Keyword filters that keep the funny/cool posts and drop the news-y or grim ones."""
import re

from .common import Post


def contains(text: str | None, keywords: list[str]) -> bool:
    """Plain words match whole words only ('war' must not match 'warm'); emoji and phrases match anywhere."""
    low = (text or "").lower()
    if not low:
        return False
    for word in keywords:
        word = word.lower()
        if word.replace(" ", "").isalpha() and word.isascii():
            if re.search(rf"\b{re.escape(word)}\b", low):
                return True
        elif word in low:
            return True
    return False


def is_serious(post: Post, cfg: dict) -> bool:
    return contains(post.text, cfg.get("blocked_keywords", []))


def is_funny(post: Post, cfg: dict) -> bool:
    return contains(post.text, cfg.get("funny_keywords", []))


def drop_serious(posts: list[Post], cfg: dict) -> list[Post]:
    if not cfg.get("skip_serious", True):
        return posts
    return [p for p in posts if not is_serious(p, cfg)]
