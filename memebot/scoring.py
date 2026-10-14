"""Outlier score: how far a post is above what its own account usually gets.

Raw views always favour the biggest pages. A clip with 300K views on a page that usually gets 40K is
a better signal that the *clip* is good than 2M views on a page that gets 5M every day.
"""
import statistics
import time

from .common import Post

KEEP_PER_ACCOUNT = 40
MIN_SAMPLES = 5
SETTLED_AFTER = 12 * 3600  # a post younger than this is still collecting views


def account_key(post: Post) -> str:
    return f"{post.platform}:{post.author.lower()}"


def annotate(posts: list[Post], state: dict, now: float | None = None):
    """Remembers each account's recent numbers in state["baseline"] and sets post.outlier."""
    now = now or time.time()
    baseline = state.setdefault("baseline", {})
    for post in posts:
        if post.popularity <= 0:
            continue
        seen = baseline.setdefault(account_key(post), {})
        seen[post.id] = [post.popularity, int(post.timestamp or now)]
    for key, seen in baseline.items():
        if len(seen) > KEEP_PER_ACCOUNT:
            newest = sorted(seen.items(), key=lambda kv: kv[1][1], reverse=True)[:KEEP_PER_ACCOUNT]
            baseline[key] = dict(newest)
    for post in posts:
        seen = baseline.get(account_key(post), {})
        usual = [views for pid, (views, ts) in seen.items() if pid != post.id and now - ts >= SETTLED_AFTER]
        if post.popularity > 0 and len(usual) >= MIN_SAMPLES:
            median = statistics.median(usual)
            if median > 0:
                post.outlier = round(post.popularity / median, 2)


def taste_bonus(post: Post, state: dict) -> float:
    """What the owner's 👍/👎 taps say about this account, as a score bonus between -1.5 and +1.5."""
    votes = state.get("taste", {}).get(account_key(post), 0)
    return max(-3, min(3, votes)) * 0.5


def is_muted(post: Post, state: dict, threshold: int = -3) -> bool:
    return state.get("taste", {}).get(account_key(post), 0) <= threshold
