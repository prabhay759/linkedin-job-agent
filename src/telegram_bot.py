from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import httpx

from src.config import Config
from src.store import load_applications, get_pending_applications

log = logging.getLogger(__name__)

_POLL_TIMEOUT = 30
_RETRY_SLEEP = 5
_CONFLICT_SLEEP = 15  # 409: another instance still running, wait longer


class TelegramCommandBot:
    def __init__(
        self,
        config: Config,
        config_lock: threading.Lock,
        on_hunt: Optional[Callable[[], None]] = None,
        on_yes_reply: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._config = config
        self._config_lock = config_lock
        self._on_hunt = on_hunt
        self._on_yes_reply = on_yes_reply
        self._offset = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def _api(self) -> str:
        return f"https://api.telegram.org/bot{self._config.telegram_bot_token}"

    def start(self) -> threading.Thread:
        self._running = True
        self._clear_webhook()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="telegram-bot")
        self._thread.start()
        return self._thread

    def _clear_webhook(self) -> None:
        """Drop any webhook and close other sessions so long-polling can start cleanly."""
        try:
            httpx.post(
                f"{self._api}/deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=10,
            )
            log.info("Webhook cleared")
        except Exception as e:
            log.warning("Could not clear webhook: %s", e)

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        log.info("Telegram bot polling started")
        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    message = update.get("message") or update.get("edited_message")
                    if message:
                        self._handle(message)
            except Exception as e:
                log.warning("Telegram poll error: %s", e)
                time.sleep(_RETRY_SLEEP)

    def _get_updates(self) -> list:
        try:
            r = httpx.get(
                f"{self._api}/getUpdates",
                params={"offset": self._offset, "timeout": _POLL_TIMEOUT, "allowed_updates": ["message"]},
                timeout=_POLL_TIMEOUT + 5,
            )
            if r.status_code == 409:
                # Another bot instance is still polling — wait for it to die
                log.warning("409 Conflict: another bot instance running, waiting %ds...", _CONFLICT_SLEEP)
                time.sleep(_CONFLICT_SLEEP)
                return []
            r.raise_for_status()
            return r.json().get("result", [])
        except Exception:
            return []

    def _handle_document(self, message: dict) -> None:
        """Save an uploaded JSON cookie file to data/linkedin_cookies.json."""
        doc = message.get("document", {})
        if not doc.get("file_name", "").endswith(".json"):
            self._reply(
                "Please send a *.json* cookie file.\n\n"
                "How to export: open linkedin.com in Chrome → install *Cookie-Editor* extension "
                "→ Export → All → save as .json → send here."
            )
            return
        try:
            # Step 1: get the file path from Telegram
            r = httpx.get(
                f"{self._api}/getFile",
                params={"file_id": doc["file_id"]},
                timeout=10,
            )
            r.raise_for_status()
            file_path = r.json()["result"]["file_path"]

            # Step 2: download the file content
            token = self._config.telegram_bot_token
            r2 = httpx.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=30,
            )
            r2.raise_for_status()

            # Step 3: validate JSON
            try:
                cookies = json.loads(r2.text)
            except Exception:
                self._reply("Invalid JSON. Please export cookies in JSON format.")
                return

            # Step 4: save
            from pathlib import Path as _Path
            cookies_path = _Path("data/linkedin_cookies.json")
            cookies_path.parent.mkdir(parents=True, exist_ok=True)
            cookies_path.write_text(r2.text)
            self._reply(f"✅ Saved *{len(cookies)}* LinkedIn cookies. Next /hunt will use them.")
        except Exception as e:
            self._reply(f"Failed to save cookies: {e}")

    def _handle(self, message: dict) -> None:
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self._config.telegram_chat_id:
            return

        # Document upload → cookie import
        if message.get("document"):
            self._handle_document(message)
            return

        text = (message.get("text") or "").strip()

        # YES reply detection — must come before command parsing
        if text.upper() == "YES" and message.get("reply_to_message"):
            reply_mid = message["reply_to_message"].get("message_id")
            if reply_mid and self._on_yes_reply:
                self._on_yes_reply(reply_mid)
                self._reply("Got it — applying now...")
                return

        if text.upper() == "NO" and message.get("reply_to_message"):
            reply_mid = message["reply_to_message"].get("message_id")
            self._handle_no_reply(reply_mid)
            return

        if not text.startswith("/"):
            return

        parts = text.split(None, 1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""

        handlers = {
            "/hunt": self._cmd_hunt,
            "/status": self._cmd_status,
            "/history": self._cmd_history,
            "/setprofile": self._cmd_setprofile,
            "/setcookies": self._cmd_setcookies,
            "/help": self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler:
            handler(arg)
        else:
            self._reply(f"Unknown command: {cmd}. Use /help.")

    def _reply(self, text: str) -> None:
        try:
            httpx.post(
                f"{self._api}/sendMessage",
                json={
                    "chat_id": self._config.telegram_chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except Exception as e:
            log.warning("Bot reply failed: %s", e)

    def _handle_no_reply(self, reply_mid: Optional[int]) -> None:
        if not reply_mid:
            return
        from src.store import get_application_by_telegram_message_id, upsert_application
        app = get_application_by_telegram_message_id(reply_mid)
        if app and app.status == "pending_confirmation":
            app.status = "rejected_by_user"
            upsert_application(app)
            self._reply(f"Skipped: *{app.job_title}* at {app.company}")

    def _cmd_hunt(self, _: str) -> None:
        self._reply("Triggering immediate job scan...")
        if self._on_hunt:
            t = threading.Thread(target=self._on_hunt, daemon=True)
            t.start()

    def _cmd_status(self, _: str) -> None:
        with self._config_lock:
            kw = ", ".join(self._config.job_keywords)
            loc = ", ".join(self._config.job_locations)
            min_s = self._config.min_score
            interval = self._config.scan_interval_minutes

        apps = load_applications()
        pending = sum(1 for a in apps if a.status == "pending_confirmation")
        applied = sum(1 for a in apps if a.status == "applied")
        total = len(apps)

        self._reply(
            f"*Agent Status*\n\n"
            f"Keywords: `{kw}`\nLocations: `{loc}`\n"
            f"Min score: {min_s}/10 | Interval: {interval}min\n\n"
            f"Applications: {applied} applied, {pending} pending, {total} total"
        )

    def _cmd_history(self, _: str) -> None:
        apps = sorted(load_applications(), key=lambda a: a.created_at, reverse=True)[:10]
        if not apps:
            self._reply("No applications yet.")
            return
        lines = ["*Recent Applications*\n"]
        for a in apps:
            emoji = {"applied": "✅", "failed": "❌", "expired": "⏰", "rejected_by_user": "🚫", "applying": "⏳"}.get(a.status, "🔄")
            lines.append(f"{emoji} {a.job_title} @ {a.company} (score {a.score})")
        self._reply("\n".join(lines))

    def _cmd_setprofile(self, arg: str) -> None:
        url = arg.strip()
        if not url.startswith("https://www.linkedin.com/in/"):
            self._reply("Please provide a valid LinkedIn profile URL, e.g.:\n`/setprofile https://www.linkedin.com/in/yourname/`")
            return
        with self._config_lock:
            self._config.linkedin_profile_url = url
        self._reply(f"Profile URL updated to:\n`{url}`")

    def _cmd_setcookies(self, _: str) -> None:
        self._reply(
            "*Import LinkedIn Cookies*\n\n"
            "Send a JSON cookie file directly to this chat.\n\n"
            "How to export from Chrome:\n"
            "1. Log into linkedin.com in your browser\n"
            "2. Install *Cookie-Editor* extension\n"
            "3. Click the extension → *Export* → *Export All* (JSON)\n"
            "4. Save the file and send it here\n\n"
            "The agent will use these cookies to apply — no password login needed."
        )

    def _cmd_help(self, _: str) -> None:
        self._reply(
            "*LinkedIn Job Agent Commands*\n\n"
            "/hunt — trigger immediate job scan\n"
            "/status — show config and application stats\n"
            "/history — show last 10 applications\n"
            "/setprofile `<url>` — update your LinkedIn profile URL\n"
            "/setcookies — import browser cookies (fixes checkpoint errors)\n"
            "/help — show this message\n\n"
            "Reply *YES* to a job confirmation to apply.\n"
            "Reply *NO* to skip a job."
        )
