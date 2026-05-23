from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Set

from src.models import Application

log = logging.getLogger(__name__)

DATA_DIR = Path("data")
SEEN_JOBS_FILE = DATA_DIR / "seen_jobs.json"
APPLICATIONS_FILE = DATA_DIR / "applications.json"
MAX_SEEN = 10_000


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "pdfs").mkdir(exist_ok=True)


# ── Seen jobs ──────────────────────────────────────────────────────────────

def load_seen_job_ids() -> Set[str]:
    _ensure_data_dir()
    if not SEEN_JOBS_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    except Exception:
        return set()


def save_seen_job_ids(ids: Set[str]) -> None:
    _ensure_data_dir()
    trimmed = list(ids)[-MAX_SEEN:]
    SEEN_JOBS_FILE.write_text(json.dumps(trimmed, indent=2))


# ── Applications ───────────────────────────────────────────────────────────

def _deserialize(raw: dict) -> Application:
    return Application(**{k: v for k, v in raw.items() if k in Application.__dataclass_fields__})


def load_applications() -> List[Application]:
    _ensure_data_dir()
    if not APPLICATIONS_FILE.exists():
        return []
    try:
        return [_deserialize(r) for r in json.loads(APPLICATIONS_FILE.read_text())]
    except Exception:
        return []


def _save_applications(apps: List[Application]) -> None:
    _ensure_data_dir()
    APPLICATIONS_FILE.write_text(json.dumps([asdict(a) for a in apps], indent=2))


def upsert_application(app: Application) -> None:
    apps = load_applications()
    apps = [a for a in apps if a.id != app.id]
    apps.append(app)
    _save_applications(apps)


def get_application_by_telegram_message_id(message_id: int) -> Optional[Application]:
    for app in load_applications():
        if app.telegram_message_id == message_id:
            return app
    return None


def get_pending_applications() -> List[Application]:
    return [a for a in load_applications() if a.status == "pending_confirmation"]


def get_applications_by_status(status: str) -> List[Application]:
    return [a for a in load_applications() if a.status == status]
