from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class JobListing:
    id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    is_easy_apply: bool
    scraped_at: str
    apply_url: Optional[str] = None
    score: Optional[int] = None
    summary_bullets: Optional[List[str]] = None


@dataclass
class Application:
    id: str
    job_id: str
    job_title: str
    company: str
    job_url: str
    score: int
    status: str  # pending_confirmation | applying | applied | rejected_by_user | failed | expired
    created_at: str
    apply_url: Optional[str] = None
    cv_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    confirmed_at: Optional[str] = None
    applied_at: Optional[str] = None
    error: Optional[str] = None
    telegram_message_id: Optional[int] = None


@dataclass
class Skill:
    domain: str
    name: str
    code: str
    created_at: str
    last_used_at: Optional[str] = None
    success_count: int = 0
    failure_count: int = 0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 1.0
