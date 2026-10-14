"""Run with:  venv\\Scripts\\python -m unittest discover tests

No network: every test works on hand-made posts.
"""
import json
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memebot import ai, combos, filters, render, scoring, tgchannels  # noqa: E402
from memebot.common import Post  # noqa: E402

CFG = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
NOW = time.time()


def post(platform="tiktok", pid="1", author="acc", text="", views=10_000, age_hours=20.0, video=True) -> Post:
    return Post(platform=platform, id=pid, author=author, url=f"https://example.com/{pid}", text=text,
                views=views, timestamp=int(NOW - age_hours * 3600), is_video=video)


class Filters(unittest.TestCase):
    def test_whole_words_only(self):
        self.assertFalse(filters.contains("a warm day", ["war"]))
        self.assertTrue(filters.contains("the war goes on", ["war"]))
        self.assertFalse(filters.contains("فوتبال امشب", ["فوت"]))
        self.assertTrue(filters.contains("خبر فوت او", ["فوت"]))

    def test_one_weak_word_is_not_funny(self):
        f = CFG["filters"]
        self.assertFalse(filters.is_funny(post(text="bro went to the store"), f))
        self.assertTrue(filters.is_funny(post(text="bro this cat is insane"), f))
        self.assertTrue(filters.is_funny(post(text="me at 3am 😂"), f))

    def test_slang_is_not_news(self):
        f = CFG["filters"]
        self.assertTrue(filters.is_serious(post(text="Man found dead after crash"), f))
        self.assertFalse(filters.is_serious(post(text="i'm dead 💀 he really said that"), f))
        self.assertTrue(filters.is_serious(post(text="the war is funny lol"), f))  # hard words always block


class Scoring(unittest.TestCase):
    def test_outlier_is_relative_to_the_account(self):
        state = {}
        usual = [post(pid=str(i), author="small", views=40_000, age_hours=30 + i) for i in range(6)]
        hit = post(pid="hit", author="small", views=320_000, age_hours=5)
        scoring.annotate(usual + [hit], state, NOW)
        self.assertAlmostEqual(hit.outlier, 8.0)
        self.assertAlmostEqual(usual[0].outlier, 1.0)

    def test_no_score_without_enough_history(self):
        state = {}
        posts = [post(pid=str(i), author="new", views=1000, age_hours=30) for i in range(3)]
        scoring.annotate(posts, state, NOW)
        self.assertIsNone(posts[0].outlier)

    def test_small_account_breakout_beats_big_account_ordinary_day(self):
        breakout = post(views=300_000)
        breakout.outlier = 6.0
        ordinary = post(views=1_000_000)
        ordinary.outlier = 0.4
        self.assertGreater(breakout.heat, ordinary.heat)

    def test_history_is_capped(self):
        state = {}
        scoring.annotate([post(pid=str(i), views=100 + i, age_hours=i) for i in range(80)], state, NOW)
        self.assertEqual(len(state["baseline"]["tiktok:acc"]), scoring.KEEP_PER_ACCOUNT)

    def test_votes(self):
        state = {"taste": {"tiktok:acc": -3}}
        self.assertTrue(scoring.is_muted(post(), state))
        self.assertEqual(scoring.taste_bonus(post(author="other"), state), 0)


class Combos(unittest.TestCase):
    def setUp(self):
        self.funny = lambda p: filters.is_funny(p, CFG["filters"])

    def test_headline_is_not_a_caption(self):
        news = post("x", text="Studio confirms the sequel will release next year")
        joke = post("x", text="me when the wifi drops for 2 seconds 😭")
        self.assertEqual(combos.caption_quality(news, False), 0.0)
        self.assertGreater(combos.caption_quality(joke, True), 4)

    def test_pairs_use_each_item_once_and_skip_same_author(self):
        tweets = [post("x", "t1", "joker", "me when the wifi drops for 2 seconds 😭"),
                  post("x", "t2", "joker2", "pov: you said you'd sleep early 💀")]
        clips = [post("tiktok", "c1", "clipper", "wait for it 😂 funny fail", views=900_000),
                 post("youtube", "c2", "joker", "funny cat fails compilation lol", views=500_000)]
        pairs = combos.find_pairs(tweets, clips, CFG["combos"], is_funny=self.funny)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len({p["video"].key for p in pairs}), 2)
        self.assertEqual(len({p["tweet"].key for p in pairs}), 2)
        for p in pairs:
            self.assertNotEqual(p["tweet"].author, p["video"].author)

    def test_votes_change_the_order(self):
        tweets = [post("x", "t1", "joker", "me when the wifi drops for 2 seconds 😭")]
        clips = [post("tiktok", "c1", "liked", "wait for it 😂", views=100_000),
                 post("tiktok", "c2", "disliked", "wait for it 😂", views=120_000)]
        state = {"taste": {"tiktok:liked": 3, "tiktok:disliked": -2}}
        pairs = combos.find_pairs(tweets, clips, CFG["combos"], is_funny=self.funny,
                                  bonus=lambda p: scoring.taste_bonus(p, state))
        self.assertEqual(pairs[0]["video"].author, "liked")

    def test_ads_are_recognised(self):
        self.assertTrue(combos.is_ad(post(text="Use code MEME for 10% off")))


class Ai(unittest.TestCase):
    def setUp(self):
        self.lines = [post("x", "t0", "a", "line zero"), post("x", "t1", "b", "line one")]
        self.clips = [post("tiktok", "c0", "c"), post("tiktok", "c1", "d")]

    def answer(self, *pairs):
        return json.dumps({"pairs": [dict(zip(("clip_id", "caption_id", "caption_en", "caption_local", "score"), p))
                                     for p in pairs]})

    def test_answer_becomes_pairs(self):
        raw = self.answer(("c1", "t0", "line zero", "خط صفر", 8), ("c0", "", "my own line", "خط خودم", 7))
        pairs = ai.parse_pairs(raw, self.lines, self.clips, 6)
        self.assertEqual([p["video"].id for p in pairs], ["c1", "c0"])
        self.assertEqual(pairs[0]["tweet"].id, "t0")
        self.assertIsNone(pairs[1]["tweet"])
        self.assertEqual(pairs[0]["local"], "خط صفر")

    def test_bad_references_low_scores_and_repeats_are_dropped(self):
        raw = self.answer(("c9", "t0", "x", "x", 9), ("zzz", "t0", "x", "x", 9), ("c0", "t1", "x", "x", 3),
                          ("c1", "t1", "ok", "ok", 9), ("c1", "t0", "again", "again", 9))
        pairs = ai.parse_pairs(raw, self.lines, self.clips, 6)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["video"].id, "c1")

    def test_off_without_a_key(self):
        import os
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertFalse(ai.enabled({"enabled": True}))
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved


class Render(unittest.TestCase):
    def test_caption_band(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            for text in ("me when the wifi drops for 2 seconds 😭", "وقتی وای‌فای دو ثانیه قطع میشه"):
                out = Path(tmp) / "band.png"
                height = render.caption_band(text, out)
                if height is None:
                    self.skipTest("no usable font on this machine")
                with Image.open(out) as image:
                    self.assertEqual(image.size, (render.WIDTH, height))
                    self.assertEqual(height % 2, 0)
                    self.assertLess(image.convert("L").getextrema()[0], 60)  # something black was drawn

    def test_make_meme(self):
        import subprocess

        ffmpeg = render.ffmpeg_exe()
        if not ffmpeg:
            self.skipTest("no ffmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=540x960:rate=25",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-shortest",
                            "-c:v", "libx264", "-c:a", "aac", str(clip)], check=True, stdin=subprocess.DEVNULL)
            meme = render.make_meme(clip, "وقتی وای‌فای دو ثانیه قطع میشه")
            self.assertIsNotNone(meme)
            frame = Path(tmp) / "frame.png"
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(meme), "-frames:v", "1", str(frame)],
                           check=True, stdin=subprocess.DEVNULL)
            from PIL import Image

            with Image.open(frame) as image:
                self.assertEqual(image.width, render.WIDTH)
                self.assertEqual(image.getpixel((5, 5))[:3], (255, 255, 255))  # the caption band is on top
                if len(sys.argv) > 1 and sys.argv[-1] == "--keep-frame":
                    image.save(ROOT / "meme-sample.png")

    def test_wall_of_text_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(render.caption_band("word " * 200, Path(tmp) / "band.png"))


class TelegramChannels(unittest.TestCase):
    PAGE = '''<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="funchan/41">
<a class="tgme_widget_message_video_player" href="https://t.me/funchan/41"><i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn.example/t.jpg')"></i></a>
<div class="tgme_widget_message_text js-message_text" dir="auto">خیلی خنده‌دار بود<br/>@funchan</div>
<span class="tgme_widget_message_views">12.5K</span><time datetime="2026-10-13T20:00:00+00:00" class="time">20:00</time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="funchan/42">
<div class="tgme_widget_message_text js-message_text" dir="auto">فقط متن</div>
<span class="tgme_widget_message_views">900</span><time datetime="2026-10-13T21:00:00+00:00" class="time">21:00</time></div></div>'''

    def test_parse(self):
        class Page:
            text = self.PAGE

        original = tgchannels.http_get
        tgchannels.http_get = lambda url, **kw: Page()
        try:
            posts = tgchannels.fetch_channel("@funchan")
        finally:
            tgchannels.http_get = original
        self.assertEqual([p.id for p in posts], ["funchan/41", "funchan/42"])
        first, second = posts
        self.assertTrue(first.is_video)
        self.assertEqual(first.views, 12500)
        self.assertEqual(first.text, "خیلی خنده‌دار بود")
        self.assertEqual(first.thumbnail, "https://cdn.example/t.jpg")
        self.assertEqual(first.url, "https://t.me/funchan/41")
        self.assertFalse(second.is_video)
        self.assertEqual(second.key, "telegram:funchan/42")


if __name__ == "__main__":
    unittest.main()
