"""Finds content that belongs together: a tweet that fits a video, or several posts about one thing.

Matching runs on names and hashtags — "Ed Sheeran", "Minecraft", "#oktoberfest" — not on ordinary
words, because two posts sharing "during" have nothing to do with each other while two posts sharing
"MrBeast" almost always do.
"""
import math
import re
from dataclasses import dataclass, field

from .common import Post

WORD_RE = re.compile(r"[a-z][a-z']{2,}")
TAG_RE = re.compile(r"#(\w{3,})")
NAME_RE = re.compile(r"\b([A-Z][a-zA-Z0-9'&.-]{2,})\b")
EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]")
URL_RE = re.compile(r"https?://\S+")
SENTENCE_START_RE = re.compile(r"(?:^|[.!?\n]\s*)([A-Z][a-zA-Z0-9'&.-]{2,})")

STOPWORDS = {
    "the", "and", "for", "you", "your", "are", "was", "were", "this", "that", "with", "from", "have",
    "has", "had", "but", "not", "all", "can", "get", "got", "his", "her", "him", "she", "they", "them",
    "their", "out", "who", "what", "when", "where", "why", "how", "just", "like", "one", "two", "now",
    "new", "our", "some", "any", "than", "then", "there", "here", "been", "being", "does", "did",
    "doing", "into", "over", "about", "after", "before", "more", "most", "much", "very", "will",
    "would", "could", "should", "its", "it's", "i'm", "don't", "didn't", "you're", "we're", "im",
    "dont", "cant", "wont", "isnt", "aint", "yes", "yeah", "nah", "too", "own", "off", "only", "also",
    "back", "down", "still", "even", "ever", "never", "every", "want", "need", "make", "made", "says",
    "said", "say", "see", "saw", "know", "think", "going", "gonna", "guys", "video", "watch", "via",
    "follow", "link", "bio", "comment", "comments", "share", "tag", "tags", "credit", "tiktok",
    "instagram", "twitter", "shorts", "short", "subscribe", "full", "part", "day", "days", "today",
    "time", "year", "years", "people", "man", "men", "woman", "women", "guy", "girl", "boy", "while",
    "during", "because", "might", "many", "thing", "things", "first", "last", "next", "another",
    "really", "actually", "literally", "something", "anything", "everything", "nothing", "someone",
    "everyone", "nobody", "again", "around", "between", "both", "each", "few", "other", "others",
    "same", "such", "these", "those", "through", "under", "until", "upon", "which", "whose", "whom",
    "him", "himself", "herself", "itself", "myself", "yourself", "themselves", "let", "lets", "put",
    "take", "took", "give", "gave", "come", "came", "went", "goes", "keep", "kept", "look", "looks",
    "looking", "feel", "feels", "felt", "find", "found", "way", "ways", "life", "lot", "lots", "bit",
    "good", "great", "best", "better", "bad", "worst", "big", "little", "old", "young", "real",
    "true", "sure", "right", "wrong", "hard", "easy", "long", "short", "high", "low", "top", "end",
    "start", "started", "stop", "help", "work", "works", "working", "home", "world", "everybody",
}
# Words that start sentences or headlines and look like names but are not.
NOT_NAMES = {"the", "this", "that", "when", "what", "why", "how", "who", "his", "her", "they", "she",
             "and", "but", "not", "you", "your", "our", "its", "for", "from", "with", "just", "now",
             "new", "one", "two", "all", "can", "got", "get", "pov", "nah", "yes", "bro", "wait",
             "after", "before", "every", "never", "always", "still", "even", "really", "meanwhile",
             "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "january", "february", "march", "april", "may", "june", "july", "august", "september",
             "october", "november", "december", "christmas", "halloween", "internet", "online"}
# Lines that read like a news headline: never the funny part of a meme.
NEWSY = ("confirms", "confirmed", "announces", "announced", "announcement", "reportedly",
         "according to", "reveals", "revealed", "reports", "sources say", "officially",
         "statement", "final score", "signs with", "box office", "premiere", "responds",
         "speaks out", "addresses the", "expected to", "has been named", "set to release",
         "will release", "interview", "study finds", "research shows", "data shows")
# The unmistakable "this is a joke" markers.
LAUGH = ("😂", "🤣", "💀", "😭", "🤡", "😹", "💔", "🥲", "lol", "lmao", "lmfao")

# A sponsored line is never the funny part.
AD_MARKERS = ("#ad", "sponsor", "promo code", "use code", "discount code", "casino", "roobet",
              "betting", "odds", "link in bio", "swipe up", "available now at", "shop now")


@dataclass
class Bag:
    words: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    emojis: set[str] = field(default_factory=set)

    @property
    def topics(self) -> set[str]:
        return self.names | self.tags


def bag_of(text: str | None) -> Bag:
    text = URL_RE.sub(" ", text or "")
    low = text.lower()
    tags = {t for t in TAG_RE.findall(low) if t not in STOPWORDS}
    words = {w for w in WORD_RE.findall(low) if w not in STOPWORDS} - tags
    # A capitalized word only counts as a name when it is not merely starting a sentence.
    starters = {s.lower() for s in SENTENCE_START_RE.findall(text)}
    names = set()
    for raw in NAME_RE.findall(text):
        name = raw.lower().strip(".")
        if len(name) < 3 or name in NOT_NAMES or name in STOPWORDS:
            continue
        if name in starters and text.count(raw) < 2:
            continue
        names.add(name)
    return Bag(words, tags, names - tags, set(EMOJI_RE.findall(text)))


def topic_weights(bags: list[Bag]) -> dict[str, float]:
    """A topic shared by two posts out of two hundred is a real coincidence; one in twenty is not."""
    total = max(len(bags), 1)
    seen: dict[str, int] = {}
    for bag in bags:
        for token in bag.topics:
            seen[token] = seen.get(token, 0) + 1
    return {token: math.log(total / count) for token, count in seen.items()}


def _similar_text(a: Bag, b: Bag) -> float:
    union = a.words | b.words
    return len(a.words & b.words) / len(union) if union else 0.0


# Openings that make a line work as a meme caption over someone else's clip.
CAPTION_PATTERNS = (
    r"^(me|my|we|us|him|her|them|bro|nah|pov|when|why|how|imagine|tell me|nobody|not me|the way|"
    r"that moment|this is|i can'?t|i'?m|you know|please|watching|trying to)\b",
    r"\bwhen (i|you|he|she|they|we|it|the|my|your|his|her|bro)\b",
    r"^nobody\s*:",
)


def caption_quality(post: Post, funny: bool) -> float:
    """How well a line would work as the text laid over a clip."""
    text = " ".join((post.text or "").split())
    low = text.lower()
    if not 12 <= len(text) <= 180 or "http" in low or any(m in low for m in AD_MARKERS):
        return 0.0
    if any(m in low for m in NEWSY):
        return 0.0  # a headline, not a punchline
    score = 1.0 + (1.5 if funny else 0.0)
    if any(re.search(p, low) for p in CAPTION_PATTERNS):
        score += 2.5
    if any(m in low for m in LAUGH):
        score += 1.5
    elif EMOJI_RE.search(text):
        score += 0.5
    if len(text) <= 90:
        score += 0.5
    if text.endswith(("?", "…", ":")):
        score += 0.3
    if sum(c.isdigit() for c in text) > 6 or "%" in text or "$" in text:
        score -= 1.5  # reads like a statistic, not a joke
    return max(score, 0.0)


def _is_meme_shaped(post: Post) -> bool:
    low = " ".join((post.text or "").split()).lower()
    return any(re.search(pattern, low) for pattern in CAPTION_PATTERNS) or any(m in low for m in LAUGH)


def find_pairs(tweets: list[Post], videos: list[Post], cfg: dict, is_funny=lambda p: False,
               require_funny: bool = True) -> list[dict]:
    """Pairs a caption-shaped tweet with a funny clip. Same topic scores higher, but is not required."""
    bags = {p.key: bag_of(p.text) for p in tweets + videos}
    weights = topic_weights(list(bags.values()))
    captions = [(p, caption_quality(p, is_funny(p))) for p in tweets]
    captions = [(p, q) for p, q in captions if q >= cfg.get("min_caption_quality", 2.5)]
    clips = [p for p in videos if p.popularity >= cfg.get("min_video_views", 5000)]
    if require_funny:
        # Both halves have to be funny on their own, or the combination will not be either.
        captions = [(p, q) for p, q in captions if is_funny(p) or _is_meme_shaped(p)]
        clips = [p for p in clips if is_funny(p) or _is_meme_shaped(p)]
    scored = []
    for tweet, quality in captions:
        for video in clips:
            if tweet.author.lower() == video.author.lower():
                continue  # the same account cross-posting is not a new combo
            a, b = bags[tweet.key], bags[video.key]
            if _similar_text(a, b) >= cfg.get("max_text_similarity", 0.4):
                continue
            shared = sorted(a.topics & b.topics)
            score = quality + math.log10(max(video.popularity, 10))
            score += 1.0 if is_funny(video) else 0.0
            score += 1.0 if _is_meme_shaped(video) else 0.0
            score += sum(weights.get(t, 1.0) for t in shared)  # same topic: a real bonus, when it happens
            scored.append({"score": score, "shared": shared, "tweet": tweet, "video": video})
    scored.sort(key=lambda c: c["score"], reverse=True)
    used_tweets, used_videos, used_authors, pairs = set(), set(), {}, []
    for combo in scored:
        author = combo["video"].author.lower()
        if combo["tweet"].key in used_tweets or combo["video"].key in used_videos or used_authors.get(author, 0) >= 2:
            continue
        used_tweets.add(combo["tweet"].key)
        used_videos.add(combo["video"].key)
        used_authors[author] = used_authors.get(author, 0) + 1
        pairs.append(combo)
    return pairs


def find_packs(posts: list[Post], cfg: dict, hot_topics: set[str] | None = None) -> list[dict]:
    """Groups posts about the same topic, from more than one platform.

    One shared word is a coincidence, so members must either share a second topic with the leading
    post or the topic must be one X is trending right now.
    """
    hot = {t.lower() for t in (hot_topics or set())}
    bags = {p.key: bag_of(p.text) for p in posts}
    weights = topic_weights(list(bags.values()))
    groups: dict[str, list[Post]] = {}
    for post in posts:
        for topic in bags[post.key].topics:
            if weights.get(topic, 0) >= cfg.get("pack_min_weight", 2.5):
                groups.setdefault(topic, []).append(post)
    packs = []
    for topic, members in groups.items():
        by_author = {}
        for p in sorted(members, key=lambda p: p.popularity, reverse=True):
            by_author.setdefault(p.author.lower(), p)  # one post per account
        members = list(by_author.values())
        if members and not any(topic in h.split() or topic == h for h in hot):
            lead = bags[members[0].key].topics
            members = [members[0]] + [m for m in members[1:] if len(bags[m.key].topics & lead) >= 2]
        if len(members) >= cfg.get("pack_min_items", 3) and len({p.platform for p in members}) >= cfg.get("pack_min_platforms", 2):
            members = members[: cfg.get("pack_max_items", 4)]
            packs.append({"topic": topic, "posts": members,
                          "score": weights[topic] * max(sum(p.popularity for p in members), 1) ** 0.25})
    packs.sort(key=lambda p: p["score"], reverse=True)
    kept, taken = [], set()
    for pack in packs:
        keys = {p.key for p in pack["posts"]}
        if len(keys & taken) > len(keys) / 2:  # mostly the same posts as a stronger pack
            continue
        taken |= keys
        kept.append(pack)
    return kept
