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

from memebot import ai, combos, filters, instagram, media, render, scoring, tgchannels, tiktok, x, youtube
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
    "telegram": "پست‌های پربازدید کانال‌های تلگرام",
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
    state = {"seen": {}, "trend_sent": {}, "combo_sent": {}, "initialized": [], "yt_ids": {}, "failures": {},
             "alerted": [], "last_trends": 0}
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as e:
            # A half-written file must not stop the bot for good; keep it for a look and start over.
            broken = path.with_suffix(".broken.json")
            path.replace(broken)
            print(f"{path.name} خراب بود ({e})؛ به {broken.name} منتقل شد و از نو شروع می‌شود.")
    return state


def save_state(state: dict, path: Path):
    now = time.time()
    state["seen"] = {k: t for k, t in state["seen"].items() if now - t < 30 * 86400}
    state["trend_sent"] = {k: t for k, t in state["trend_sent"].items() if now - t < 7 * 86400}
    state["combo_sent"] = {k: t for k, t in state.get("combo_sent", {}).items() if now - t < 7 * 86400}
    state["vote_meta"] = {k: v for k, v in state.get("vote_meta", {}).items() if now - v[1] < 14 * 86400}
    # Write aside and swap, so a run killed mid-write (sleep, shutdown) never leaves a broken file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def collect(cfg: dict, state: dict, platforms: list[str] | None = None):
    """Fetch recent posts for every configured account. Returns ({platform: {account: posts}}, {platform: [errors]})."""
    jobs = {
        "youtube": (cfg["youtube"]["channels"],
                    lambda ref: youtube.fetch_channel(youtube.resolve_channel_id(ref, state["yt_ids"]),
                                                      shorts_only=cfg["youtube"].get("shorts_only", False)), 4),
        "instagram": (cfg["instagram"]["accounts"], instagram.fetch_account, 2),
        "tiktok": (cfg["tiktok"]["accounts"], tiktok.fetch_account, 3),
        "x": (cfg["x"]["accounts"], x.fetch_account, 4),
        "telegram": (cfg["telegram"]["accounts"], tgchannels.fetch_channel, 3),
    }
    jobs = {name: job for name, job in jobs.items() if job[0]}  # a platform with no accounts is switched off
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
    scoring.annotate([p for accounts in results.values() for posts in accounts.values() for p in posts], state)
    return results, errors


def send_new_posts(results: dict, cfg: dict, state: dict, tg: Telegram, allowed: set[str] | None = None) -> int:
    settings = cfg["settings"]
    max_age = settings["max_post_age_hours"] * 3600
    now = time.time()
    sent = 0
    for platform, accounts in results.items():
        for account, posts in accounts.items():
            account_key = f"{platform}:{account.lower()}"
            fresh = sorted((p for p in posts if p.key not in state["seen"]), key=lambda p: p.timestamp or 0)
            if not cfg[platform].get("new_posts", True) or (allowed is not None and platform not in allowed):
                # This platform only feeds the trend digest and the combos (X, by default).
                for p in fresh:
                    state["seen"][p.key] = int(now)
                if account_key not in state["initialized"]:
                    state["initialized"].append(account_key)
                continue
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
                if tg.offline:
                    break  # left unseen, so the next run sends them
                try:
                    tg.send_post(p)
                    state["seen"][p.key] = int(now)
                    sent += 1
                except Exception as e:  # left unseen so the next run retries it
                    print(f"[{platform}] send failed for {p.url}: {e}")
    return sent


def send_trends(results: dict, cfg: dict, state: dict, tg: Telegram, allowed: set[str] | None = None):
    t = cfg["trends"]
    now = time.time()
    wanted = [p for p in results if allowed is None or p in allowed]

    def recent(platform):
        return [p for posts in results.get(platform, {}).values() for p in posts
                if p.timestamp and now - p.timestamp <= t["window_hours"] * 3600]

    candidates = {platform: recent(platform) for platform in wanted}
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if api_key and "youtube" in wanted:
        try:
            candidates["youtube"] = youtube.fetch_trending_api(api_key, t["youtube_query"], t["youtube_region"], t["window_hours"])
        except Exception as e:
            print(f"[youtube] trending API failed, using watched channels: {e}")
    if "tiktok" in wanted:
        try:
            candidates["tiktok"] += tiktok.fetch_trending(t["tiktok_region"])
        except Exception as e:
            print(f"[tiktok] trending feed failed: {e}")

    fcfg = cfg["filters"]
    for platform, posts in candidates.items():
        if tg.offline:
            break
        unique = {p.key: p for p in posts if p.key not in state["trend_sent"] and not combos.is_ad(p)}.values()
        pool = filters.drop_serious(list(unique), fcfg)
        if fcfg.get("funny_first", True):
            # Keep only the funny/cool ones, unless that leaves too few to fill the digest.
            funny = [p for p in pool if filters.is_funny(p, fcfg)]
            if len(funny) >= t["top_n"]:
                pool = funny
        top, per_author = [], {}
        # heat, not raw views: a small page's breakout beats a huge page's ordinary day.
        for p in sorted(pool, key=lambda p: p.heat, reverse=True):
            if len(top) < t["top_n"] and per_author.get(p.author.lower(), 0) < 2:
                top.append(p)
                per_author[p.author.lower()] = per_author.get(p.author.lower(), 0) + 1
        topics = []
        if platform == "x" and "x" in wanted:
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
    # Telegram down or nothing collected (no internet): the next run tries again instead of waiting hours.
    if not tg.offline and any(candidates.values()):
        state["last_trends"] = int(now)


def vote_buttons(post) -> dict | None:
    """👍/👎 under a suggestion; the taps are read back on the next run and steer what gets picked."""
    if len(f"vote:down:{post.key}".encode()) > 64:  # Telegram's limit for a button's payload
        return None
    return {"inline_keyboard": [[{"text": "👍 خوب بود", "callback_data": f"vote:up:{post.key}"},
                                 {"text": "👎 به درد نخورد", "callback_data": f"vote:down:{post.key}"}]]}


def read_votes(state: dict, tg: Telegram):
    votes, state["tg_offset"] = tg.read_votes(state.get("tg_offset", 0))
    taste = state.setdefault("taste", {})
    for key, delta in votes:
        account = state.get("vote_meta", {}).get(key)
        if account:
            taste[account[0]] = taste.get(account[0], 0) + delta
    if votes:
        print(f"votes read: {len(votes)}")


def send_pair(combo: dict, ccfg: dict, tg: Telegram):
    """One ready-to-post meme: the clip with the line already on it, plus the line as copyable text."""
    tweet, video = combo["tweet"], combo["video"]
    text = combo.get("text") or " ".join(tweet.text.split())
    local = combo.get("local") or text
    topic = f" · موضوع مشترک: {esc(', '.join(combo['shared']))}" if combo["shared"] else ""
    badge = " · 🤖 انتخاب هوش مصنوعی" if combo.get("by_ai") else ""
    original = f"\n🌐 متن اصلی: <code>{esc(text)}</code>" if local != text else ""
    links = f"🔗 <a href=\"{esc(video.url)}\">کلیپ</a>"
    if tweet:
        links += f" · <a href=\"{esc(tweet.url)}\">توییت</a>"
    caption = (
        f"🎬 <b>ترکیب پیشنهادی</b>{topic}{badge}\n\n"
        f"✍️ متن روی کلیپ (لمس کن تا کپی شود):\n<code>{esc(local)}</code>{original}\n\n"
        f"📹 کلیپ از {author_link(video)} · {stats_line(video)}\n{links}"
    )
    buttons = vote_buttons(video)
    clip = media.download_video(video.url, ccfg.get("max_video_mb", 45))
    try:
        # The finished meme when it can be made; otherwise the plain clip, which is still usable.
        meme = render.make_meme(clip, local, ccfg.get("max_video_mb", 45)) if clip and ccfg.get("render_meme", True) else None
        sent = tg.send_video_file(meme or clip, caption, video.url, buttons) if clip else False
    finally:
        if clip:
            shutil.rmtree(clip.parent, ignore_errors=True)
    if not sent:  # download blocked or upload refused: the links still do the job
        tg.send_card(caption, video.thumbnail, video.url, buttons)


def pick_pairs(tweets: list, clips: list, cfg: dict, state: dict) -> list[dict]:
    """Claude's pairing when a key is set and it answers; the keyword matcher otherwise."""
    ccfg, fcfg, acfg = cfg["combos"], cfg["filters"], cfg.get("ai", {})
    funny = lambda p: filters.is_funny(p, fcfg)  # noqa: E731
    bonus = lambda p: scoring.taste_bonus(p, state)  # noqa: E731
    if ai.enabled(acfg):
        ranked = sorted((p for p in clips if p.popularity >= ccfg.get("min_video_views", 5000)),
                        key=lambda p: combos.clip_score(p, funny, bonus), reverse=True)
        lines = sorted(((combos.caption_quality(p, funny(p)), p) for p in tweets), key=lambda qp: qp[0], reverse=True)
        pairs = ai.make_pairs([p for q, p in lines if q > 0], ranked, ccfg.get("pairs_per_run", 3), acfg)
        if pairs is not None:
            print(f"[ai] {len(pairs)} pairs proposed")
            return pairs
    return combos.find_pairs(tweets, clips, ccfg, is_funny=funny,
                             require_funny=ccfg.get("funny_only", True), bonus=bonus)


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
    read_votes(state, tg)
    used = state.setdefault("combo_sent", {})
    clip_platforms = ccfg.get("clip_platforms", ["tiktok", "instagram", "youtube"])
    # A tweet or a clip is suggested once; otherwise the same hit clip comes back every run with a new line.
    tweets = [p for p in posts if p.platform == "x" and p.key not in used]
    clips = [p for p in posts if p.platform in clip_platforms and p.is_video and p.key not in used
             and not scoring.is_muted(p, state)]  # an account the owner keeps voting down

    sent = 0
    for combo in pick_pairs(tweets, clips, cfg, state):
        if sent >= ccfg.get("pairs_per_run", 3) or tg.offline:
            break
        try:
            send_pair(combo, ccfg, tg)
        except Exception as e:
            print(f"[combos] ارسال ترکیب نشد: {e}")
            continue
        video = combo["video"]
        used[video.key] = int(now)
        if combo["tweet"]:
            used[combo["tweet"].key] = int(now)
        state.setdefault("vote_meta", {})[video.key] = [scoring.account_key(video), int(now)]
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
        if packs >= ccfg.get("packs_per_run", 1) or tg.offline:
            break
        key = f"pack:{pack['topic']}"
        if key in used:
            continue
        try:
            send_pack(pack, tg)
        except Exception as e:
            print(f"[combos] ارسال بسته نشد: {e}")
            continue
        used[key] = int(now)
        packs += 1
    print(f"combos sent: {sent} pairs, {packs} packs")
    if not tg.offline and posts:
        state["last_combos"] = int(now)


def track_failures(results: dict, errors: dict, state: dict, tg: Telegram, alert_for: set[str] | None = None):
    """Warns once when a source has fully failed FAIL_ALERT_AFTER runs in a row, and once when it is back.

    `alert_for` limits the messages to the platforms this run is responsible for, so the PC run does not
    repeat warnings about sources the GitHub run already watches.
    """
    state.setdefault("last_error", {})
    alerted = set(state.get("alerted", []))
    for platform in list(state["failures"]):
        if platform not in errors:  # not collected in this run, so its old counter means nothing
            state["failures"].pop(platform, None)
            state["last_error"].pop(platform, None)
            alerted.discard(platform)
    down = {platform for platform, errs in errors.items() if errs and not results[platform]}
    if len(errors) > 1 and down == set(errors):
        # Every source failed together: this machine's internet or VPN is down, not the sources.
        print("همه‌ی منابع با هم خطا دادند؛ مشکل از اینترنت یا VPN همین سیستم است، نه از منابع.")
        state["alerted"] = sorted(alerted)
        return
    for platform, errs in errors.items():
        # Kept in state.json so a failure on the GitHub servers can be diagnosed later.
        state["last_error"][platform] = errs[0][:500] if errs else ""
        state["failures"][platform] = state["failures"].get(platform, 0) + 1 if platform in down else 0
        if alert_for is not None and platform not in alert_for:
            continue
        try:
            if platform in down and state["failures"][platform] >= FAIL_ALERT_AFTER and platform not in alerted:
                hint = ""
                if "blocked" in errs[0].lower() or "cloudflare" in errs[0].lower():
                    hint = "\nسایت منبع آی‌پی VPN را بلاک کرده. سرور یا لوکیشن VPN را عوض کن."
                tg.send_text(f"⚠️ منبع <b>{PLATFORM_LABELS[platform]}</b> "
                             f"{state['failures'][platform]} بار پشت سر هم کار نکرد.{hint}\n"
                             f"<code>{esc(errs[0][:300])}</code>")
                alerted.add(platform)
            elif platform not in down and platform in alerted:
                tg.send_text(f"✅ منبع <b>{PLATFORM_LABELS[platform]}</b> دوباره کار می‌کند.")
                alerted.discard(platform)
        except TelegramError as e:  # not marked as sent, so the next run tries again
            print(f"[{platform}] هشدار خرابی فرستاده نشد: {e}")
    state["alerted"] = sorted(alerted)


def setup_output():
    """The scheduled task runs pythonw.exe, which gives the process no console at all."""
    if sys.stdout is None or sys.stderr is None:
        log = ROOT / "local-run.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            log.replace(log.with_name(log.name + ".1"))  # the previous stretch stays readable
        stream = open(log, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
        print(f"\n===== {time.strftime('%Y-%m-%d %H:%M')} =====")
    else:
        sys.stdout.reconfigure(encoding="utf-8")


def main():
    setup_output()
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
    parser.add_argument("--uploads", default="", help="only these platforms may send new-post messages (others are still collected for combos)")
    parser.add_argument("--trend-platforms", default="", help="only these platforms may appear in the trend digest")
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

    def platforms(flag: str, value: str) -> set[str] | None:
        names = {p.strip().lower() for p in value.split(",") if p.strip()}
        if unknown := names - set(PLATFORM_LABELS):
            parser.error(f"{flag}: unknown platform {', '.join(sorted(unknown))} (use {','.join(PLATFORM_LABELS)})")
        return names or None

    only, skip = platforms("--only", args.only), platforms("--skip", args.skip) or set()
    uploads, trend_platforms = platforms("--uploads", args.uploads), platforms("--trend-platforms", args.trend_platforms)
    chosen = [p for p in PLATFORM_LABELS if (only is None or p in only) and p not in skip]

    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    cfg.setdefault("telegram", {}).setdefault("accounts", [])  # a config from before this section existed
    state_file = Path(args.state) if Path(args.state).is_absolute() else ROOT / args.state
    state = load_state(state_file)
    results, errors = collect(cfg, state, chosen)

    if args.sample:
        for platform, accounts in results.items():
            posts = [p for ps in accounts.values() for p in ps]
            if posts:
                tg.send_post(max(posts, key=lambda p: p.timestamp or 0), badge="🧪 تست")
                print(f"[{platform}] پیام تست فرستاده شد")
        return

    try:
        if not state["initialized"]:
            counts = " · ".join(f"{PLATFORM_LABELS[p]}: {len(cfg[p]['channels' if p == 'youtube' else 'accounts'])}" for p in chosen)
            tg.send_text(f"✅ <b>ربات میم فعال شد</b>\n\nاکانت‌های زیرنظر: {counts}\n"
                         f"از این به بعد پست‌های جدید همینجا فرستاده میشه و هر {cfg['settings']['trends_every_hours']} ساعت "
                         f"یک گزارش ترند می‌گیری.")

        sent = send_new_posts(results, cfg, state, tg, uploads)
        print(f"new posts sent: {sent}")
        if not args.no_trends and (args.trends or time.time() - state["last_trends"] >= cfg["settings"]["trends_every_hours"] * 3600 - 600):
            send_trends(results, cfg, state, tg, trend_platforms)
        combo_cfg = cfg.get("combos", {})
        if not args.no_combos and combo_cfg.get("enabled", True) and (
                args.combos or time.time() - state.get("last_combos", 0) >= combo_cfg.get("every_hours", 3) * 3600 - 600):
            send_combos(results, cfg, state, tg)
        track_failures(results, errors, state, tg, alert_for=uploads)
    finally:
        # Saved even when Telegram dropped halfway, or everything already sent would be sent again next run.
        if not args.dry_run:
            save_state(state, state_file)
    if tg.offline:  # reported as a failed run, so the scheduled task's LastTaskResult shows it
        raise tg.offline


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
