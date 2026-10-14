import html
import json
import time

from curl_cffi import CurlMime

from .common import PLATFORM_LABELS, Post, http_get, human, new_session


class TelegramError(Exception):
    pass


class TelegramConnectionError(TelegramError):
    """Could not reach api.telegram.org at all (dropped VPN, filtering, no internet)."""


PROFILE_URLS = {
    "instagram": "https://www.instagram.com/{}/",
    "tiktok": "https://www.tiktok.com/@{}",
    "x": "https://x.com/{}",
    "telegram": "https://t.me/{}",
}


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ago(ts: int | None) -> str:
    if not ts:
        return ""
    mins = max(0, int((time.time() - ts) / 60))
    if mins < 60:
        return f"{mins} دقیقه پیش"
    if mins < 1440:
        return f"{mins // 60} ساعت پیش"
    return f"{mins // 1440} روز پیش"


def stats_line(post: Post) -> str:
    parts = []
    if post.views is not None:
        parts.append(f"👁 {human(post.views)}")
    if post.likes is not None:
        parts.append(f"❤️ {human(post.likes)}")
    if post.outlier and post.outlier >= 2:
        parts.append(f"🚀 {post.outlier:.1f}× معمولِ این اکانت")
    if post.timestamp:
        parts.append(f"🕒 {ago(post.timestamp)}")
    return " · ".join(parts)


def author_link(post: Post) -> str:
    tpl = PROFILE_URLS.get(post.platform)
    if not tpl:
        return f"<b>{esc(post.author)}</b>"
    return f'<a href="{esc(tpl.format(post.author))}">@{esc(post.author)}</a>'


class Telegram:
    ATTEMPTS = 4

    def __init__(self, token: str | None, chat_id: str | None, dry_run: bool = False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        # Set once Telegram stays unreachable through every retry. From then on each call fails at once,
        # instead of every remaining post spending minutes on its own retries.
        self.offline: TelegramConnectionError | None = None
        self.session = new_session()

    def call(self, method: str, data: dict, upload: tuple[str, str, str, bytes] | None = None) -> dict:
        """`upload` is (field, filename, content type, bytes) for sendPhoto / sendVideo."""
        if self.offline:
            raise self.offline
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        last_network_error = None
        for attempt in range(self.ATTEMPTS):
            if attempt and last_network_error:
                print(f"  اتصال به تلگرام قطع شد، تلاش دوباره ({attempt + 1}/{self.ATTEMPTS})…")
                time.sleep(3 * attempt)
            try:
                if upload is not None:
                    field, filename, content_type, blob = upload
                    mp = CurlMime()
                    try:
                        for k, v in data.items():
                            # A form field cannot carry an object; Telegram takes it as a JSON string.
                            mp.addpart(name=k, data=(json.dumps(v) if isinstance(v, dict) else str(v)).encode())
                        mp.addpart(name=field, filename=filename, content_type=content_type, data=blob)
                        r = self.session.post(url, multipart=mp, timeout=(20, 180))
                    finally:
                        mp.close()
                else:
                    r = self.session.post(url, json=data, timeout=(20, 60))
                body = r.json()
            except Exception as e:  # reset connection / TLS / DNS / a proxy's HTML error page — usually a dropped VPN
                last_network_error = TelegramConnectionError(f"ارتباط با سرور تلگرام برقرار نشد ({type(e).__name__})")
                continue
            if body.get("ok"):
                return body
            retry = (body.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and retry:
                last_network_error = None
                time.sleep(retry + 1)
                continue
            if r.status_code >= 500:
                last_network_error = TelegramConnectionError(f"سرور تلگرام خطا داد (HTTP {r.status_code})")
                continue
            raise TelegramError(f"Telegram {method} failed: {body.get('description')}")
        if last_network_error:
            self.offline = last_network_error
            raise last_network_error
        raise TelegramError(f"Telegram {method}: too many retries")

    def send_text(self, text: str, preview_url: str | None = None, buttons: dict | None = None):
        if self.dry_run:
            print("\n--- TELEGRAM (text) ---\n" + text)
            return
        data = {"chat_id": self.chat_id, "text": text[:4096], "parse_mode": "HTML"}
        data["link_preview_options"] = {"url": preview_url} if preview_url else {"is_disabled": True}
        if buttons:
            data["reply_markup"] = buttons
        self.call("sendMessage", data)
        time.sleep(1.1)

    def send_card(self, caption: str, thumbnail: str | None, preview_url: str | None, buttons: dict | None = None):
        """A picture with the caption under it, or plain text when the picture cannot be fetched."""
        if self.dry_run:
            print(f"\n--- TELEGRAM (card, thumb={bool(thumbnail)}) ---\n{caption}")
            return
        extra = {"reply_markup": buttons} if buttons else {}
        image = None
        if thumbnail:
            try:
                r = http_get(thumbnail, retries=1, timeout=20)
                image = (r.content, r.headers.get("content-type") or "image/jpeg")
            except Exception:
                image = None
        if image:
            try:
                self.call("sendPhoto", {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML", **extra},
                          upload=("photo", "preview", image[1], image[0]))
                time.sleep(1.1)
                return
            except TelegramConnectionError:
                raise  # plain text would not get through either
            except TelegramError as e:
                print(f"  ارسال عکس نشد، به جایش متن فرستاده می‌شود: {e}")
        self.send_text(caption, preview_url=preview_url, buttons=buttons)

    def send_video_file(self, path, caption: str, preview_url: str | None = None, buttons: dict | None = None) -> bool:
        """Uploads the clip itself. Returns False when Telegram refuses it, so the caller can fall back."""
        if self.dry_run:
            print(f"\n--- TELEGRAM (video {path.name}) ---\n{caption}")
            return True
        extra = {"reply_markup": buttons} if buttons else {}
        try:
            self.call("sendVideo", {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML",
                                    "supports_streaming": "true", **extra},
                      upload=("video", path.name, "video/mp4", path.read_bytes()))
            time.sleep(1.1)
            return True
        except TelegramConnectionError:
            raise
        except TelegramError as e:
            print(f"  ارسال فایل ویدیو نشد: {e}")
            return False

    def send_post(self, post: Post, badge: str = "🆕 پست جدید"):
        head = f"{PLATFORM_LABELS[post.platform]} · {author_link(post)} · {badge}"
        body = f"<i>{esc(shorten(post.text, 600))}</i>" if post.text else ""
        link = f'🔗 <a href="{esc(post.url)}">{"دیدن ویدیو" if post.is_video else "دیدن پست"}</a>'
        caption = "\n\n".join(p for p in (head, body, "\n".join(filter(None, (stats_line(post), link)))) if p)
        self.send_card(caption, post.thumbnail, post.url)

    def read_votes(self, offset: int) -> tuple[list[tuple[str, int]], int]:
        """Collects the 👍/👎 taps since the last run. Returns ([(post key, +1 or -1)], next offset).

        The bot only runs every half hour, so a tap is acknowledged then: the buttons under that
        message are replaced by a single "noted" one.
        """
        if self.dry_run or self.offline:
            return [], offset
        try:
            updates = self.call("getUpdates", {"offset": offset, "timeout": 0}).get("result", [])
        except TelegramError as e:  # e.g. a webhook is set on this bot; the votes are simply not read
            print(f"  رأی‌ها خوانده نشد: {e}")
            return [], offset
        votes = []
        for upd in updates:
            offset = max(offset, upd["update_id"] + 1)
            query = upd.get("callback_query") or {}
            data = query.get("data") or ""
            message = query.get("message") or {}
            chat = str((message.get("chat") or {}).get("id", ""))
            if not data.startswith("vote:") or chat != str(self.chat_id):
                continue  # someone else's chat must not steer this bot's taste
            _, sign, key = data.split(":", 2)
            votes.append((key, 1 if sign == "up" else -1))
            noted = {"inline_keyboard": [[{"text": "👍 ثبت شد" if sign == "up" else "👎 ثبت شد", "callback_data": "noop"}]]}
            for method, payload in (
                ("answerCallbackQuery", {"callback_query_id": query.get("id")}),
                ("editMessageReplyMarkup", {"chat_id": chat, "message_id": message.get("message_id"), "reply_markup": noted}),
            ):
                try:
                    self.call(method, payload)
                except TelegramConnectionError:
                    return votes, offset
                except TelegramError:
                    pass  # the tap is older than Telegram keeps a query open; the vote still counts
        return votes, offset

    def chat_ids(self) -> list[tuple[str, str]]:
        try:
            body = self.session.get(f"https://api.telegram.org/bot{self.token}/getUpdates", timeout=30).json()
        except Exception as e:
            raise TelegramConnectionError(f"ارتباط با سرور تلگرام برقرار نشد ({type(e).__name__})")
        found = {}
        for upd in body.get("result", []):
            msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                found[str(chat["id"])] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        return list(found.items())
