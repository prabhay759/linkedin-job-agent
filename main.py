#!/usr/bin/env python3
"""
LinkedIn Job Application Agent
On-demand job hunter: send /hunt via Telegram to scan LinkedIn, score jobs
against your profile, generate tailored CVs + cover letters, and apply after
your Telegram confirmation.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import time

from src.config import load_config, Config
from src.models import Application, JobListing
from src.store import (
    load_seen_job_ids,
    save_seen_job_ids,
    upsert_application,
    get_pending_applications,
    get_application_by_telegram_message_id,
)
from src.scraper import scrape_profile, fetch_jobs
from src.analyzer import score_jobs_batch, generate_cv_content, generate_cover_letter_content
from src.document_generator import generate_cv_pdf, generate_cover_letter_pdf
from src.notifier import send_job_card, send_application_result, send_startup_message, send_error
from src.telegram_bot import TelegramCommandBot
from src.applicator import Applicator
from src.skill_manager import SkillManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")


class _TelegramLogHandler(logging.Handler):
    """Forwards ERROR+ log records to Telegram so you don't need to open Railway."""

    def __init__(self, token: str, chat_id: str) -> None:
        super().__init__(level=logging.ERROR)
        self._token = token
        self._chat_id = chat_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            import httpx as _httpx
            msg = self.format(record)
            _httpx.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": f"🚨 *Agent Error*\n```\n{msg[:3500]}\n```",
                    "parse_mode": "Markdown",
                },
                timeout=5,
            )
        except Exception:
            pass  # never let the log handler crash the agent

# Global state
_scan_lock = threading.Lock()
_config_lock = threading.Lock()
_apply_lock = threading.Lock()
_config: Optional[Config] = None
_skill_manager: Optional[SkillManager] = None
_seen_ids: set = set()
_last_profile_text: str = ""  # cached from most recent scan


def _personal_info() -> dict:
    return {
        "name": _config.your_full_name,
        "email": _config.your_email,
        "phone": _config.your_phone,
        "location": _config.your_location,
        "linkedin_url": _config.linkedin_profile_url,
    }


def run_scan() -> None:
    """Full scan cycle: scrape → score → generate docs → send confirmations."""
    global _seen_ids
    if not _scan_lock.acquire(blocking=False):
        log.info("Scan already in progress, skipping")
        return

    try:
        log.info("=== Starting job scan ===")
        with _config_lock:
            cfg = _config

        # 1. Scrape user profile (falls back to cache if LinkedIn blocks)
        global _last_profile_text
        log.info("Fetching profile: %s", cfg.linkedin_profile_url)
        try:
            profile_text = asyncio.run(scrape_profile(cfg.linkedin_profile_url))
            _last_profile_text = profile_text
            log.info("Profile ready: %d chars", len(profile_text))
        except Exception as e:
            log.error("Profile unavailable: %s", e)
            send_error(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                f"Profile unavailable: {e}\n\nUse `/setprofile <text>` in Telegram to paste your CV/profile directly and skip LinkedIn scraping.",
            )
            return

        # 2. Fetch new jobs
        log.info("Fetching jobs for keywords: %s", cfg.job_keywords)
        try:
            jobs = asyncio.run(fetch_jobs(cfg, _seen_ids))
            log.info("Fetched %d new jobs", len(jobs))
        except Exception as e:
            log.error("Job fetching failed: %s", e)
            send_error(cfg.telegram_bot_token, cfg.telegram_chat_id, f"Job fetching failed: {e}")
            return

        if not jobs:
            log.info("No new jobs found")
            from src.notifier import send_message
            send_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                "No new jobs found matching your keywords and locations.",
            )
            return

        # 3. Score jobs
        log.info("Scoring %d jobs (min score: %d)...", len(jobs), cfg.min_score)
        try:
            qualified_jobs = score_jobs_batch(jobs, profile_text, cfg.openrouter_api_key, cfg.min_score)
            log.info("%d jobs qualify (score >= %d)", len(qualified_jobs), cfg.min_score)
        except Exception as e:
            log.error("Job scoring failed: %s", e)
            return

        # Mark all fetched jobs as seen (even if below threshold)
        for job in jobs:
            _seen_ids.add(job.id)
        save_seen_job_ids(_seen_ids)

        # 4. For each qualified job: generate docs + send confirmation
        if not qualified_jobs:
            from src.notifier import send_message
            send_message(
                cfg.telegram_bot_token, cfg.telegram_chat_id,
                f"No match found — scanned {len(jobs)} job(s), none scored {cfg.min_score}/10 or above.",
            )
            log.info("No jobs met the minimum score threshold")
            return

        for job in qualified_jobs:
            try:
                _process_job(job, profile_text, cfg)
            except Exception as e:
                log.error("Failed to process job %s: %s", job.id, e)

        log.info("=== Scan complete ===")
    finally:
        _scan_lock.release()


def _process_job(job: JobListing, profile_text: str, cfg: Config) -> None:
    """Send job card to Telegram. CV/CL are generated only when user replies YES."""
    log.info("Sending card: %s @ %s (score %d)", job.title, job.company, job.score)

    # Create a lightweight pending record — no CV/CL yet
    app = Application(
        id=str(uuid.uuid4()),
        job_id=job.id,
        job_title=job.title,
        company=job.company,
        job_url=job.url,
        apply_url=job.apply_url,
        score=job.score,
        status="pending_confirmation",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    upsert_application(app)

    message_id = send_job_card(cfg.telegram_bot_token, cfg.telegram_chat_id, job)
    if message_id:
        app.telegram_message_id = message_id
        upsert_application(app)
        log.info("Card sent for %s (message_id=%d)", job.title, message_id)
    else:
        log.warning("Failed to send card for %s", job.title)


def on_yes_reply(confirmation_message_id: int) -> None:
    """Called by TelegramCommandBot when user replies YES to a job card."""
    with _config_lock:
        cfg = _config

    app = get_application_by_telegram_message_id(confirmation_message_id)
    if not app:
        log.warning("YES reply for unknown message_id=%d", confirmation_message_id)
        return
    if app.status != "pending_confirmation":
        log.info("YES reply for app %s already in status %s", app.id, app.status)
        return

    log.info("User confirmed: %s @ %s — generating CV + cover letter", app.job_title, app.company)
    app.status = "generating"
    app.confirmed_at = datetime.now(timezone.utc).isoformat()
    upsert_application(app)

    job = _rebuild_job_from_app(app)

    t = threading.Thread(
        target=lambda: asyncio.run(_generate_and_apply(app, job, cfg)),
        daemon=True,
        name=f"apply-{app.id[:8]}",
    )
    t.start()


def _rebuild_job_from_app(app: Application) -> JobListing:
    return JobListing(
        id=app.job_id,
        title=app.job_title,
        company=app.company,
        location="",
        url=app.job_url,
        apply_url=app.apply_url,
        description="",
        is_easy_apply=(app.apply_url is None),
        scraped_at=app.created_at,
    )


async def _generate_and_apply(app: Application, job: JobListing, cfg: Config) -> None:
    """Generate CV + cover letter for the confirmed job, then apply."""
    from src.notifier import send_message, send_document

    personal = _personal_info()
    profile_text = _last_profile_text
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Generate CV + cover letter content via LLM
    try:
        send_message(cfg.telegram_bot_token, cfg.telegram_chat_id,
                     f"Generating tailored CV and cover letter for *{job.title}* at {job.company}...")
        cv_data = generate_cv_content(job, profile_text, cfg.openrouter_api_key, personal)
        cl_data = generate_cover_letter_content(job, profile_text, cfg.openrouter_api_key, personal)
        cl_data["date"] = today
        cv_path = await generate_cv_pdf(cv_data, job.id)
        cl_path = await generate_cover_letter_pdf(cl_data, job.id)
    except Exception as e:
        log.error("Doc generation failed for %s: %s", job.title, e)
        app.status = "failed"
        app.error = f"Doc generation failed: {e}"
        upsert_application(app)
        send_error(cfg.telegram_bot_token, cfg.telegram_chat_id,
                   f"Failed to generate documents for {job.title}: {e}")
        return

    app.cv_path = str(cv_path)
    app.cover_letter_path = str(cl_path)
    app.status = "applying"
    upsert_application(app)

    # Apply
    applicator = Applicator(cfg, _skill_manager)
    success = await applicator.apply(app, job)

    app.status = "applied" if success else "failed"
    app.applied_at = datetime.now(timezone.utc).isoformat()
    upsert_application(app)

    send_application_result(cfg.telegram_bot_token, cfg.telegram_chat_id, app)
    log.info("Application %s: %s @ %s", app.status, app.job_title, app.company)


def expire_pending_check() -> None:
    """Expire pending confirmations older than CONFIRMATION_TIMEOUT_HOURS."""
    with _config_lock:
        timeout_hours = _config.confirmation_timeout_hours

    cutoff = datetime.now(timezone.utc) - timedelta(hours=timeout_hours)
    for app in get_pending_applications():
        created = datetime.fromisoformat(app.created_at.replace("Z", "+00:00"))
        if created < cutoff:
            app.status = "expired"
            upsert_application(app)
            log.info("Expired confirmation for %s @ %s", app.job_title, app.company)


def main() -> None:
    global _config, _skill_manager, _seen_ids

    log.info("LinkedIn Job Agent starting up...")

    # 1. Load and validate config
    try:
        _config = load_config()
        _config.validate()
    except Exception as e:
        log.error("Config error: %s", e)
        sys.exit(1)

    # 2. Attach Telegram log handler so errors appear in Telegram, not just Railway
    tg_handler = _TelegramLogHandler(_config.telegram_bot_token, _config.telegram_chat_id)
    tg_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logging.getLogger().addHandler(tg_handler)

    # 3. Init dependencies
    _skill_manager = SkillManager(_config.openrouter_api_key)
    _seen_ids = load_seen_job_ids()
    log.info("Loaded %d seen job IDs, %d skills", len(_seen_ids), len(_skill_manager.list_skills()))

    # 4. Start Telegram command bot
    bot = TelegramCommandBot(
        config=_config,
        config_lock=_config_lock,
        on_hunt=run_scan,
        on_yes_reply=on_yes_reply,
    )
    bot.start()
    log.info("Telegram bot started")

    # 5. Send startup notification
    send_startup_message(
        _config.telegram_bot_token,
        _config.telegram_chat_id,
        _config.job_keywords,
        _config.job_locations,
    )

    # 6. Background thread: expire stale confirmations every 5 minutes
    def _expiry_loop() -> None:
        while True:
            time.sleep(300)
            try:
                expire_pending_check()
            except Exception as e:
                log.warning("Expiry check failed: %s", e)

    threading.Thread(target=_expiry_loop, daemon=True, name="expiry-check").start()

    # 7. Keep process alive — all work is driven by Telegram /hunt commands
    log.info("Ready. Send /hunt via Telegram to start a job scan.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutting down")
        bot.stop()


if __name__ == "__main__":
    main()
