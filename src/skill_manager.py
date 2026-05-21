from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.models import Skill

log = logging.getLogger(__name__)

SKILLS_FILE = Path("data/skills.json")
REGEN_THRESHOLD = 0.50


class SkillManager:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._skills: Dict[str, Skill] = {}
        self._lock = threading.Lock()
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not SKILLS_FILE.exists():
            return
        try:
            data = json.loads(SKILLS_FILE.read_text())
            for domain, raw in data.get("skills", {}).items():
                self._skills[domain] = Skill(**{k: v for k, v in raw.items() if k in Skill.__dataclass_fields__})
            log.info("Loaded %d skills from %s", len(self._skills), SKILLS_FILE)
        except Exception as e:
            log.error("Failed to load skills: %s", e)

    def _save(self) -> None:
        SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        import dataclasses
        data = {
            "version": 1,
            "skills": {d: dataclasses.asdict(s) for d, s in self._skills.items()},
        }
        SKILLS_FILE.write_text(json.dumps(data, indent=2))

    # ── Accessors ──────────────────────────────────────────────────────────

    def get_skill(self, domain: str) -> Optional[Skill]:
        with self._lock:
            return self._skills.get(domain)

    def needs_regen(self, domain: str) -> bool:
        skill = self.get_skill(domain)
        if not skill:
            return True
        total = skill.success_count + skill.failure_count
        if total < 3:
            return False  # not enough data yet, keep current
        return skill.success_rate < REGEN_THRESHOLD

    def list_skills(self) -> List[Skill]:
        with self._lock:
            return sorted(self._skills.values(), key=lambda s: s.success_rate, reverse=True)

    # ── Execution ──────────────────────────────────────────────────────────

    def _compile_skill(self, code: str):
        namespace: dict = {}
        exec(compile(code, "<skill>", "exec"), namespace)  # noqa: S102
        if "apply" not in namespace:
            raise KeyError("Generated code does not define 'apply' function")
        return namespace["apply"]

    async def execute_skill(
        self,
        domain: str,
        page,
        job,
        config,
    ) -> bool:
        skill = self.get_skill(domain)
        if not skill:
            return False
        try:
            apply_fn = self._compile_skill(skill.code)
            await apply_fn(page, job, config)
            self.record_success(domain)
            return True
        except Exception as e:
            log.warning("Skill execution failed for %s: %s", domain, e)
            self.record_failure(domain)
            return False

    async def learn_and_execute(
        self,
        domain: str,
        page,
        job,
        config,
        page_html: str,
        max_retries: int = 3,
    ) -> bool:
        from src.analyzer import generate_ats_skill_code

        error_context: Optional[str] = None

        for attempt in range(1, max_retries + 1):
            log.info("Generating ATS skill for %s (attempt %d/%d)", domain, attempt, max_retries)
            code = generate_ats_skill_code(domain, page_html, self._api_key, error_context)
            if not code:
                log.error("LLM returned no code for %s", domain)
                continue

            try:
                apply_fn = self._compile_skill(code)
            except (SyntaxError, KeyError) as e:
                error_context = f"Compilation error: {e}"
                log.warning("Skill compilation failed (attempt %d): %s", attempt, e)
                continue

            try:
                await apply_fn(page, job, config)
                # Success — save the skill
                with self._lock:
                    now = datetime.now(timezone.utc).isoformat()
                    skill = Skill(
                        domain=domain,
                        name=f"{domain.split('.')[0].title()} Apply",
                        code=code,
                        created_at=now,
                        last_used_at=now,
                        success_count=1,
                        failure_count=0,
                    )
                    self._skills[domain] = skill
                    self._save()
                log.info("New skill learned and saved for %s", domain)
                return True
            except Exception as e:
                error_context = f"Runtime error: {type(e).__name__}: {e}"
                log.warning("Skill runtime failed (attempt %d): %s", attempt, e)

        log.error("Could not learn skill for %s after %d attempts", domain, max_retries)
        return False

    def record_success(self, domain: str) -> None:
        with self._lock:
            skill = self._skills.get(domain)
            if skill:
                skill.success_count += 1
                skill.last_used_at = datetime.now(timezone.utc).isoformat()
                self._save()

    def record_failure(self, domain: str) -> None:
        with self._lock:
            skill = self._skills.get(domain)
            if skill:
                skill.failure_count += 1
                skill.last_used_at = datetime.now(timezone.utc).isoformat()
                self._save()
