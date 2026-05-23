from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import httpx

from src.models import Application, JobListing

log = logging.getLogger(__name__)

_TIMEOUT = 30


def _api(token: str) -> str:
    return f"https://api.telegram.org/bot{token}"


def send_message(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
    reply_to_message_id: Optional[int] = None,
    disable_web_page_preview: bool = True,
) -> Optional[int]:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    try:
        r = httpx.post(f"{_api(token)}/sendMessage", json=payload, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()["result"]["message_id"]
    except Exception as e:
        log.error("send_message failed: %s", e)
        return None


def send_document(
    token: str,
    chat_id: str,
    file_path: Path,
    caption: str = "",
    reply_to_message_id: Optional[int] = None,
) -> Optional[int]:
    data: dict = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_to_message_id:
        data["reply_to_message_id"] = str(reply_to_message_id)
    try:
        with open(file_path, "rb") as f:
            r = httpx.post(
                f"{_api(token)}/sendDocument",
                data=data,
                files={"document": (file_path.name, f, "application/pdf")},
                timeout=60,
            )
        r.raise_for_status()
        return r.json()["result"]["message_id"]
    except Exception as e:
        log.error("send_document failed: %s", e)
        return None


def send_job_confirmation(
    token: str,
    chat_id: str,
    job: JobListing,
    app: Application,
) -> Optional[int]:
    bullets = "\n".join(f"• {b}" for b in (job.summary_bullets or []))
    apply_type = "LinkedIn Easy Apply" if job.is_easy_apply else "External Application"
    text = (
        f"*Job Match Found!* Score: *{job.score}/10*\n\n"
        f"*Position:* {job.title}\n"
        f"*Company:* {job.company}\n"
        f"*Location:* {job.location}\n"
        f"*Type:* {apply_type}\n\n"
        f"*Key Points:*\n{bullets}\n\n"
        f"[View on LinkedIn]({job.url})"
    )
    mid = send_message(token, chat_id, text)
    if not mid:
        return None

    # Send CV and cover letter as documents
    if app.cv_path and Path(app.cv_path).exists():
        send_document(token, chat_id, Path(app.cv_path), caption="Tailored CV", reply_to_message_id=mid)

    final_mid = None
    if app.cover_letter_path and Path(app.cover_letter_path).exists():
        final_mid = send_document(
            token,
            chat_id,
            Path(app.cover_letter_path),
            caption="Cover Letter — *Reply YES to this message to apply* (expires in 24h)",
            reply_to_message_id=mid,
        )

    return final_mid or mid


def send_application_result(
    token: str,
    chat_id: str,
    app: Application,
) -> None:
    status_emoji = "✅" if app.status == "applied" else "❌"
    status_text = "Application submitted!" if app.status == "applied" else f"Application failed: {app.error}"
    text = (
        f"{status_emoji} *{status_text}*\n\n"
        f"*Role:* {app.job_title} at {app.company}\n"
        f"[View Job]({app.job_url})"
    )
    mid = send_message(token, chat_id, text)
    if app.status == "applied":
        if app.cv_path and Path(app.cv_path).exists():
            send_document(token, chat_id, Path(app.cv_path), caption="CV used", reply_to_message_id=mid)
        if app.cover_letter_path and Path(app.cover_letter_path).exists():
            send_document(token, chat_id, Path(app.cover_letter_path), caption="Cover letter used", reply_to_message_id=mid)


def send_startup_message(token: str, chat_id: str, keywords: list, locations: list) -> None:
    kw = ", ".join(keywords)
    loc = ", ".join(locations)
    send_message(
        token,
        chat_id,
        f"*LinkedIn Job Agent ready*\n\nKeywords: `{kw}`\nLocations: `{loc}`\n\n"
        f"Send /hunt to start a job scan.\n"
        f"Commands: /hunt /status /history /setprofile /help",
    )


def send_error(token: str, chat_id: str, text: str) -> None:
    send_message(token, chat_id, f"⚠️ *Error:* {text}")
