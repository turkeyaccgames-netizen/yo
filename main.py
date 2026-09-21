"""Meme bot: new uploads + trending digests from YouTube, Instagram, TikTok and X, delivered to Telegram."""
import argparse
import json
import os
import shutil
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memebot import combos, filters, instagram, media, tiktok, x, youtube
from memebot.common import PLATFORM_LABELS
from memebot.telegram import Telegram, TelegramConnectionError, TelegramError, author_link, esc, shorten, stats_line

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
FAIL_ALERT_AFTER = 3  # consecutive fully-failed runs before a warning is sent
TREND_TITLES = {
    "youtube": "ویدیوهای پربازدید یوتیوب",
    "instagram": "پست‌های پرلایک اینستاگرام",
    "tiktok": "ویدیوهای ترند تیک‌تاک",
    "x": "توییت‌های پربازدید X",
}


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_state(path: Path) -> dict:
    state = {"seen": {}, "trend_sent": {}, "initialized": [], "yt_ids": {}, "failures": {}, "last_trends": 0}
    if path.exists():
        state.update(json.loads(path.read_text(encoding="utf-8")))
    return state


def save_state(state: dict, path: Path):
    now = time.time()
    state["seen"] = {k: t for k, t in state["seen"].items() if now - t < 30 * 86400}
    state["trend_sent"] = {k: t for k, t in state["trend_sent"].items() if now - t < 7 * 86400}
    state["combo_sent"] = {k: t for k, t in state.get("combo_sent", {}).items() if now - t < 7 * 86400}
    path.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def collect(cfg: dict, state: dict, platforms: list[str] | None = None):
    """Fetch recent posts for every configured account. Returns ({platform: {account: posts}}, {platform: [errors]})."""
    jobs = {
        "youtube": (cfg["youtube"]["channels"],
                    lambda ref: youtube.fetch_channel(youtube.resolve_channel_id(ref, state["yt_ids"]),
                                                      shorts_only=cfg["youtube"].get("shorts_only", False)), 4),
        "instagram": (cfg["instagram"]["accounts"], instagram.fetch_account, 2),
        "tiktok": (cfg["tiktok"]["accounts"], tiktok.fetch_account, 3),
        "x": (cfg["x"]["accounts"], x.fetch_account, 4),
    }
    if platforms is not None:
        jobs = {name: job for name, job in jobs.items() if name in platforms}
    results, errors = {}, {}
    for platform, (accounts, fetch, workers) in jobs.items():
        results[platform], errors[platform] = {}, []

        def run(account, fetch=fetch):
            try:
                return account, fetch(account), None
            except Exception as e:
                return account, None, e

        with ThreadPoolExecutor(workers) as pool:
            for account, posts, err in pool.map(run, accounts):
                if err:
                    errors[platform].append(f"{account}: {err}")
                    print(f"[{platform}] {account}: ERROR {str(err)[:200]}")
                else:
                    results[platform][account] = posts
                    print(f"[{platform}] {account}: {len(posts)} posts")
    return results, errors


def send_new_posts(results: dict, cfg: dict, state: dict, tg: Telegram) -> int:
    settings = cfg["settings"]
    max_age = settings["max_post_age_hours"] * 3600
    now = time.time()
    sent = 0
    for platform, accounts in results.items():
        for account, posts in accounts.items():
            account_key = f"{platform}:{account.lower()}"
            fresh = sorted((p for p in posts if p.key not in state["seen"]), key=lambda p: p.timestamp or 0)
            if account_key not in state["initialized"]:
                # First time we see this account: remember its current posts without sending them.
                state["initialized"].append(account_key)
                for p in fresh:
                    state["seen"][p.key] = int(now)
                continue
            sendable = [p for p in fresh if not p.timestamp or now - p.timestamp <= max_age]
            sendable = filters.drop_serious(sendable, cfg["filters"])
            sendable = sendable[-settings["max_new_per_account"]:]
            for p in fresh:
                if p not in sendable:
                    state["seen"][p.key] = int(now)
            for p in sendable:
                try:
                    tg.send_post(p)
                    state["seen"][p.key] = int(now)
                    sent += 1
                except Exception as e:  # left unseen so the next run retries it
                    print(f"[{platform}] send failed for {p.url}: {e}")
    return sent


def send_trends(results: dict, cfg: dict, state: dict, tg: Telegram):
    t = cfg["trends"]
    now = time.time()

    def recent(platform):
        return [p for posts in results.get(platform, {}).values() for p in posts
                if p.timestamp and now - p.timestamp <= t["window_hours"] * 3600]

    candidates = {platform: recent(platform) for platform in results}
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if api_key and "youtube" in results:
        try:
            candidates["youtube"] = youtube.fetch_trending_api(api_key, t["youtube_query"], t["youtube_region"], t["window_hours"])
        except Exception as e:
            print(f"[youtube] trending API failed, using watched channels: {e}")
    if "tiktok" in results:
        try:
            candidates["tiktok"] += tiktok.fetch_trending(t["tiktok_region"])
        except Exception as e:
            print(f"[tiktok] trending feed failed: {e}")

    fcfg = cfg["filters"]
    for platform, posts in candidates.items():
        unique = {p.key: p for p in posts if p.key not in state["trend_sent"]}.values()
        pool = filters.drop_serious(list(unique), fcfg)
        if fcfg.get("funny_first", True):
            # Keep only the funny/cool ones, unless that leaves too few to fill the digest.
            funny = [p for p in pool if filters.is_funny(p, fcfg)]
            if len(funny) >= t["top_n"]:
                pool = funny
        top, per_author = [], {}
        for p in sorted(pool, key=lambda p: p.popularity, reverse=True):
            if len(top) < t["top_n"] and per_author.get(p.author.lower(), 0) < 2:
                top.append(p)
                per_author[p.author.lower()] = per_author.get(p.author.lower(), 0) + 1
        topics = []
        if platform == "x" and "x" in results:
            try:
                topics = x.fetch_trend_topics(t["x_topics"])
                if fcfg.get("skip_serious", True):
                    topics = [(name, ctx, url) for name, ctx, url in topics
                              if not filters.contains(f"{name} {ctx}", fcfg.get("blocked_keywords", []))]
            except Exception as e:
                print(f"[x] trend topics failed: {e}")
        if not top and not topics:
            continue
        lines = [f"🔥 <b>{TREND_TITLES[platform]}</b> ({PLATFORM_LABELS[platform]})"]
        for i, p in enumerate(top, 1):
            title = esc(shorten(p.text, 110)) or "دیدن"
            lines.append(f'\n<b>{i}.</b> {author_link(p)}\n<a href="{esc(p.url)}">{title}</a>\n{stats_line(p)}')
        if topics:
            lines.append("\n📈 <b>موضوعات ترند X:</b>")
            lines.append("\n".join(f'• <a href="{esc(url)}">{esc(name)}</a> <i>{esc(ctx)}</i>' for name, ctx, url in topics))
        try:
            tg.send_text("\n".join(lines), preview_url=top[0].url if top else None)
            for p in top:
                state["trend_sent"][p.key] = int(now)
        except Exception as e:
            print(f"[{platform}] trend digest send failed: {e}")
    state["last_trends"] = int(now)


def send_pair(combo: dict, ccfg: dict, tg: Telegram):
    """One ready-to-make meme: the clip as a real file, plus the line to lay over it."""
    tweet, video = combo["tweet"], combo["video"]
    topic = f" · موضوع مشترک: {esc(', '.join(combo['shared']))}" if combo["shared"] else ""
    caption = (
        f"🎬 <b>ترکیب پیشنهادی</b>{topic}\n\n"
        f"✍️ متن روی کلیپ (لمس کن تا کپی شود):\n<code>{esc(' '.join(tweet.text.split()))}</code>\n\n"
        f"📹 کلیپ از {author_link(video)} · {stats_line(video)}\n"
        f"🔗 <a href=\"{esc(video.url)}\">کلیپ</a> · <a href=\"{esc(tweet.url)}\">توییت</a>"
    )
    clip = media.download_video(video.url, ccfg.get("max_video_mb", 45))
    sent = tg.send_video_file(clip, caption, video.url) if clip else False
    if clip:
        shutil.rmtree(clip.parent, ignore_errors=True)
    if not sent:  # download blocked or upload refused: the links still do the job
        tg.send_card(caption, video.thumbnail, video.url)


def send_pack(pack: dict, tg: Telegram):
    lines = [f"🧩 <b>بسته‌ی هم‌موضوع: {esc(pack['topic'])}</b>",
             "چند پست درباره‌ی یک ماجرا؛ می‌توانی کنار هم تدوین کنی:"]
    for i, p in enumerate(pack["posts"], 1):
        lines.append(f"\n<b>{i}.</b> {PLATFORM_LABELS[p.platform]} {author_link(p)}\n"
                     f"<a href=\"{esc(p.url)}\">{esc(shorten(p.text, 90)) or 'دیدن'}</a>\n{stats_line(p)}")
    tg.send_text("\n".join(lines), preview_url=pack["posts"][0].url)


def send_combos(results: dict, cfg: dict, state: dict, tg: Telegram):
    ccfg, fcfg = cfg["combos"], cfg["filters"]
    now = time.time()
    posts = [p for accounts in results.values() for ps in accounts.values() for p in ps
             if p.timestamp and now - p.timestamp <= ccfg.get("window_hours", 48) * 3600]
    posts = filters.drop_serious(posts, fcfg)
    funny = lambda p: filters.is_funny(p, fcfg)  # noqa: E731
    tweets = [p for p in posts if p.platform == "x"]
    clips = [p for p in posts if p.platform != "x" and p.is_video]
    state.setdefault("combo_sent", {})

    sent = 0
    for combo in combos.find_pairs(tweets, clips, ccfg, is_funny=funny):
        if sent >= ccfg.get("pairs_per_run", 3):
            break
        key = f"{combo['tweet'].key}+{combo['video'].key}"
        if key in state["combo_sent"] or combo["tweet"].key in state["combo_sent"]:
            continue
        try:
            send_pair(combo, ccfg, tg)
        except Exception as e:
            print(f"[combos] ارسال ترکیب نشد: {e}")
            continue
        state["combo_sent"][key] = state["combo_sent"][combo["tweet"].key] = int(now)
        sent += 1

    hot = set()
    try:
        if "x" not in results:
            raise RuntimeError("X در این اجرا جمع‌آوری نشده")
        hot = {name.lstrip("#").lower() for name, _, _ in x.fetch_trend_topics(15)}
    except Exception as e:
        print(f"[combos] گرفتن موضوعات ترند نشد: {e}")
    packs = 0
    for pack in combos.find_packs(posts, ccfg, hot_topics=hot):
        if packs >= ccfg.get("packs_per_run", 1):
            break
        key = f"pack:{pack['topic']}"
        if key in state["combo_sent"]:
            continue
        try:
            send_pack(pack, tg)
        except Exception as e:
            print(f"[combos] ارسال بسته نشد: {e}")
            continue
        state["combo_sent"][key] = int(now)
        packs += 1
    print(f"combos sent: {sent} pairs, {packs} packs")
    state["last_combos"] = int(now)


def track_failures(results: dict, errors: dict, state: dict, tg: Telegram):
    state.setdefault("last_error", {})
    for platform, errs in errors.items():
        # Kept in state.json so a failure on the GitHub servers can be diagnosed later.
        state["last_error"][platform] = errs[0][:500] if errs else ""
        previous = state["failures"].get(platform, 0)
        if errs and not results[platform]:
            state["failures"][platform] = previous + 1
            if previous + 1 == FAIL_ALERT_AFTER:
                tg.send_text(f"⚠️ منبع <b>{PLATFORM_LABELS[platform]}</b> {FAIL_ALERT_AFTER} بار پشت سر هم کار نکرد.\n"
                             f"<code>{esc(errs[0][:300])}</code>")
        else:
            if previous >= FAIL_ALERT_AFTER:
                tg.send_text(f"✅ منبع <b>{PLATFORM_LABELS[platform]}</b> دوباره کار می‌کند.")
            state["failures"][platform] = 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of sending them; state is not saved")
    parser.add_argument("--trends", action="store_true", help="send the trend digest now, regardless of schedule")
    parser.add_argument("--combos", action="store_true", help="send combo suggestions now, regardless of schedule")
    parser.add_argument("--sample", action="store_true", help="send the latest post from each platform to test the bot")
    parser.add_argument("--chat-id", action="store_true", help="list chats that have messaged the bot")
    parser.add_argument("--ping", action="store_true", help="send a single test message (fast connection check)")
    parser.add_argument("--only", default="", help="check only these platforms, comma separated (youtube,instagram,tiktok,x)")
    parser.add_argument("--skip", default="", help="check every platform except these, comma separated")
    parser.add_argument("--state", default="state.json", help="state file to use (a local run keeps its own)")
    parser.add_argument("--no-trends", action="store_true", help="never send the trend digest in this run")
    parser.add_argument("--no-combos", action="store_true", help="never send combo suggestions in this run")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    tg = Telegram(token, chat_id, dry_run=args.dry_run)
    if args.chat_id:
        if not token:
            sys.exit("TELEGRAM_BOT_TOKEN is not set.")
        for cid, name in tg.chat_ids() or [("", "No messages yet: send /start to your bot first, then run again.")]:
            print(cid, name)
        return
    if not args.dry_run and not (token and chat_id):
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (.env file or GitHub Secrets).")

    if args.ping:
        tg.send_text("✅ تست اتصال: ربات به تلگرام وصل است.")
        print("پیام تست فرستاده شد.")
        return

    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    state_file = Path(args.state) if Path(args.state).is_absolute() else ROOT / args.state
    state = load_state(state_file)
    chosen = [p.strip() for p in args.only.split(",") if p.strip()] or list(PLATFORM_LABELS)
    chosen = [p for p in chosen if p not in {s.strip() for s in args.skip.split(",")}]
    results, errors = collect(cfg, state, chosen)

    if args.sample:
        for platform, accounts in results.items():
            posts = [p for ps in accounts.values() for p in ps]
            if posts:
                tg.send_post(max(posts, key=lambda p: p.timestamp or 0), badge="🧪 تست")
                print(f"[{platform}] پیام تست فرستاده شد")
        return

    if not state["initialized"]:
        counts = " · ".join(f"{PLATFORM_LABELS[p]}: {len(cfg[p]['channels' if p == 'youtube' else 'accounts'])}" for p in chosen)
        tg.send_text(f"✅ <b>ربات میم فعال شد</b>\n\nاکانت‌های زیرنظر: {counts}\n"
                     f"از این به بعد پست‌های جدید همینجا فرستاده میشه و هر {cfg['settings']['trends_every_hours']} ساعت "
                     f"یک گزارش ترند می‌گیری.")

    sent = send_new_posts(results, cfg, state, tg)
    print(f"new posts sent: {sent}")
    if not args.no_trends and (args.trends or time.time() - state["last_trends"] >= cfg["settings"]["trends_every_hours"] * 3600 - 600):
        send_trends(results, cfg, state, tg)
    combo_cfg = cfg.get("combos", {})
    if not args.no_combos and combo_cfg.get("enabled", True) and (
            args.combos or time.time() - state.get("last_combos", 0) >= combo_cfg.get("every_hours", 3) * 3600 - 600):
        send_combos(results, cfg, state, tg)
    track_failures(results, errors, state, tg)
    if not args.dry_run:
        save_state(state, state_file)


if __name__ == "__main__":
    try:
        main()
    except TelegramConnectionError as e:
        print(f"\n❌ {e}\n"
              "اگر از ایران اجرا می‌کنی، VPN را روشن کن و دوباره امتحان کن.\n"
              "روی گیت‌هاب این مشکل پیش نمی‌آید، چون سرورهای گیت‌هاب فیلتر ندارند.")
        sys.exit(1)
    except TelegramError as e:
        print(f"\n❌ تلگرام درخواست را قبول نکرد: {e}")
        if "chat not found" in str(e).lower():
            print("شماره‌ی چت درست نیست. باید یک عدد باشد، نه اسم کاربری ربات.\n"
                  "اول در تلگرام به ربات خودت /start بده، بعد این دستور را بزن تا عدد درست را ببینی:\n"
                  "  python main.py --chat-id")
        elif "unauthorized" in str(e).lower():
            print("توکن ربات در فایل .env درست نیست. دوباره از @BotFather بگیرش.")
        sys.exit(1)
