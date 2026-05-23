from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    linkedin_profile_url: str
    linkedin_email: str
    linkedin_password: str
    job_keywords: List[str]
    job_locations: List[str]
    openrouter_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    your_full_name: str
    your_phone: str
    your_email: str
    your_location: str
    min_score: int = 7
    confirmation_timeout_hours: int = 24
    max_jobs_per_scan: int = 50

    def validate(self) -> None:
        required = [
            ("linkedin_profile_url", self.linkedin_profile_url),
            ("linkedin_email", self.linkedin_email),
            ("linkedin_password", self.linkedin_password),
            ("openrouter_api_key", self.openrouter_api_key),
            ("telegram_bot_token", self.telegram_bot_token),
            ("telegram_chat_id", self.telegram_chat_id),
        ]
        missing = [name for name, val in required if not val]
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")
        if not self.job_keywords:
            raise ValueError("JOB_KEYWORDS must have at least one entry")


def load_config() -> Config:
    def _list(key: str, default: str = "") -> List[str]:
        raw = os.getenv(key, default)
        return [x.strip() for x in raw.split(",") if x.strip()]

    return Config(
        linkedin_profile_url=os.getenv("LINKEDIN_PROFILE_URL", ""),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", ""),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", ""),
        job_keywords=_list("JOB_KEYWORDS", "Software Engineer"),
        job_locations=_list("JOB_LOCATIONS", "Remote"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        your_full_name=os.getenv("YOUR_FULL_NAME", ""),
        your_phone=os.getenv("YOUR_PHONE", ""),
        your_email=os.getenv("YOUR_EMAIL", ""),
        your_location=os.getenv("YOUR_LOCATION", ""),
        min_score=int(os.getenv("MIN_SCORE", "7")),
        confirmation_timeout_hours=int(os.getenv("CONFIRMATION_TIMEOUT_HOURS", "24")),
        max_jobs_per_scan=int(os.getenv("MAX_JOBS_PER_SCAN", "50")),
    )
