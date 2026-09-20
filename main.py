"""Meme bot: new uploads + trending digests from YouTube, Instagram, TikTok and X, delivered to Telegram."""
import argparse
import json
import os
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memebot import instagram, tiktok, x, youtube
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


def load_state() -> dict:
    state = {"seen": {}, "trend_sent": {}, "initialized": [], "yt_ids": {}, "failures": {}, "last_trends": 0}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return state


def save_state(state: dict):
    now = time.time()
    state["seen"] = {k: t for k, t in state["seen"].items() if now - t < 30 * 86400}
    state["trend_sent"] = {k: t for k, t in state["trend_sent"].items() if now - t < 7 * 86400}
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def collect(cfg: dict, state: dict):
    """Fetch recent posts for every configured account. Returns ({platform: {account: posts}}, {platform: [errors]})."""
    jobs = {
        "youtube": (cfg["youtube"]["channels"], lambda ref: youtube.fetch_channel(youtube.resolve_channel_id(ref, state["yt_ids"])), 4),
        "instagram": (cfg["instagram"]["accounts"], instagram.fetch_account, 2),
        "tiktok": (cfg["tiktok"]["accounts"], tiktok.fetch_account, 3),
        "x": (cfg["x"]["accounts"], x.fetch_account, 4),
    }
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

    candidates = {platform: recent(platform) for platform in ("youtube", "instagram", "tiktok", "x")}
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if api_key:
        try:
            candidates["youtube"] = youtube.fetch_trending_api(api_key, t["youtube_query"], t["youtube_region"], t["window_hours"])
        except Exception as e:
            print(f"[youtube] trending API failed, using watched channels: {e}")
    try:
        candidates["tiktok"] += tiktok.fetch_trending(t["tiktok_region"])
    except Exception as e:
        print(f"[tiktok] trending feed failed: {e}")

    for platform, posts in candidates.items():
        unique = {p.key: p for p in posts if p.key not in state["trend_sent"]}.values()
        top, per_author = [], {}
        for p in sorted(unique, key=lambda p: p.popularity, reverse=True):
            if len(top) < t["top_n"] and per_author.get(p.author.lower(), 0) < 2:
                top.append(p)
                per_author[p.author.lower()] = per_author.get(p.author.lower(), 0) + 1
        topics = []
        if platform == "x":
            try:
                topics = x.fetch_trend_topics(t["x_topics"])
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


def track_failures(results: dict, errors: dict, state: dict, tg: Telegram):
    for platform, errs in errors.items():
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
    parser.add_argument("--sample", action="store_true", help="send the latest post from each platform to test the bot")
    parser.add_argument("--chat-id", action="store_true", help="list chats that have messaged the bot")
    parser.add_argument("--ping", action="store_true", help="send a single test message (fast connection check)")
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
    state = load_state()
    results, errors = collect(cfg, state)

    if args.sample:
        for platform, accounts in results.items():
            posts = [p for ps in accounts.values() for p in ps]
            if posts:
                tg.send_post(max(posts, key=lambda p: p.timestamp or 0), badge="🧪 تست")
                print(f"[{platform}] پیام تست فرستاده شد")
        return

    if not state["initialized"]:
        counts = " · ".join(f"{PLATFORM_LABELS[p]}: {len(cfg[p]['channels' if p == 'youtube' else 'accounts'])}" for p in PLATFORM_LABELS)
        tg.send_text(f"✅ <b>ربات میم فعال شد</b>\n\nاکانت‌های زیرنظر: {counts}\n"
                     f"از این به بعد پست‌های جدید همینجا فرستاده میشه و هر {cfg['settings']['trends_every_hours']} ساعت "
                     f"یک گزارش ترند می‌گیری.")

    sent = send_new_posts(results, cfg, state, tg)
    print(f"new posts sent: {sent}")
    if args.trends or time.time() - state["last_trends"] >= cfg["settings"]["trends_every_hours"] * 3600 - 600:
        send_trends(results, cfg, state, tg)
    track_failures(results, errors, state, tg)
    if not args.dry_run:
        save_state(state)


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
