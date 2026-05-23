from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Tuple

import httpx

from src.models import JobListing

log = logging.getLogger(__name__)

OPENROUTER_API = "https://openrouter.ai/api/v1"
_TIMEOUT = 60
_TEMPERATURE = 0.2
_FALLBACK_MODEL = "meta-llama/llama-3.1-70b-instruct:free"

# Cached after first successful detection
_selected_model: Optional[str] = None


def detect_free_model(api_key: str) -> str:
    """
    Fetch OpenRouter's model list, pick the highest-capability free model
    (ranked by context window size). Result is cached for the process lifetime.
    """
    global _selected_model
    if _selected_model:
        return _selected_model

    try:
        r = httpx.get(
            f"{OPENROUTER_API}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        r.raise_for_status()
        models = r.json().get("data", [])

        # Free = prompt cost is "0" (OpenRouter returns costs as strings)
        free = [
            m for m in models
            if str(m.get("pricing", {}).get("prompt", "1")) == "0"
        ]

        # Prefer larger context windows as a proxy for capability
        free.sort(key=lambda m: m.get("context_length", 0), reverse=True)

        if free:
            _selected_model = free[0]["id"]
            log.info(
                "Auto-selected free model: %s (context: %s tokens)",
                _selected_model,
                free[0].get("context_length", "?"),
            )
            return _selected_model
    except Exception as e:
        log.warning("Could not fetch OpenRouter models, using fallback: %s", e)

    _selected_model = _FALLBACK_MODEL
    log.info("Using fallback model: %s", _selected_model)
    return _selected_model


def _call_llm(api_key: str, system: str, user: str) -> Optional[dict]:
    model = detect_free_model(api_key)
    payload = {
        "model": model,
        "temperature": _TEMPERATURE,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        r = httpx.post(
            f"{OPENROUTER_API}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/prabhay759/linkedin-job-agent",
                "X-Title": "LinkedIn Job Agent",
            },
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        # Some models wrap JSON in markdown fences — strip them
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.error("LLM returned invalid JSON: %s", e)
        return None
    except Exception as e:
        log.error("LLM call failed: %s", e)
        return None


def _call_llm_raw(api_key: str, system: str, user: str) -> Optional[str]:
    """Like _call_llm but returns raw text — used for code generation."""
    model = detect_free_model(api_key)
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        r = httpx.post(
            f"{OPENROUTER_API}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/prabhay759/linkedin-job-agent",
                "X-Title": "LinkedIn Job Agent",
            },
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("LLM raw call failed: %s", e)
        return None


# ── Job scoring ────────────────────────────────────────────────────────────

_SCORE_SYSTEM = (
    "You are a senior technical recruiter. Given a job description and a candidate's profile, "
    "score how well the candidate fits the role on a scale of 1-10. "
    "10 = perfect match, 7 = strong match, 5 = partial match, below 5 = poor match. "
    "Respond ONLY with valid JSON, no markdown."
)

_SCORE_USER = """Profile:
{profile}

Job Title: {title}
Company: {company}
Description:
{description}

Respond with JSON:
{{"score": <int 1-10>, "reasoning": "<one sentence>", "bullets": ["<key point 1>", "<key point 2>", "<key point 3>"]}}"""


def score_job(job: JobListing, profile_text: str, api_key: str) -> Tuple[int, List[str]]:
    result = _call_llm(
        api_key,
        _SCORE_SYSTEM,
        _SCORE_USER.format(
            profile=profile_text[:2000],
            title=job.title,
            company=job.company,
            description=job.description[:2000],
        ),
    )
    if not result:
        return 0, []
    score = int(result.get("score", 0))
    bullets = result.get("bullets", [])
    return max(0, min(10, score)), bullets[:5]


def score_jobs_batch(
    jobs: List[JobListing],
    profile_text: str,
    api_key: str,
    min_score: int,
) -> List[JobListing]:
    scored = []
    for job in jobs:
        score, bullets = score_job(job, profile_text, api_key)
        job.score = score
        job.summary_bullets = bullets
        log.info("  %s @ %s → score %d/10", job.title, job.company, score)
        if score >= min_score:
            scored.append(job)
    scored.sort(key=lambda j: j.score, reverse=True)
    return scored


# ── CV content generation ──────────────────────────────────────────────────

_CV_SYSTEM = (
    "You are a professional CV writer. Given a candidate's profile and a job description, "
    "produce a tailored CV data structure. The CV should emphasise experiences and skills "
    "most relevant to this specific job. Keep it honest — don't invent experience. "
    "Respond ONLY with valid JSON, no markdown."
)

_CV_USER = """Profile:
{profile}

Target Job: {title} at {company}
Job Description:
{description}

Respond with JSON matching exactly this structure:
{{
  "headline": "<role title tailored to job>",
  "summary": "<2-3 sentence professional summary tailored to this role>",
  "experience": [
    {{"title": "...", "company": "...", "dates": "...", "bullets": ["...", "..."]}}
  ],
  "skills": ["skill1", "skill2", "..."],
  "education": [
    {{"degree": "...", "school": "...", "dates": "..."}}
  ]
}}"""


def generate_cv_content(job: JobListing, profile_text: str, api_key: str, personal: dict) -> dict:
    result = _call_llm(
        api_key,
        _CV_SYSTEM,
        _CV_USER.format(
            profile=profile_text[:2500],
            title=job.title,
            company=job.company,
            description=job.description[:2000],
        ),
    )
    base = {
        "name": personal.get("name", ""),
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "location": personal.get("location", ""),
        "linkedin_url": personal.get("linkedin_url", ""),
        "headline": job.title,
        "summary": "",
        "experience": [],
        "skills": [],
        "education": [],
    }
    if result:
        base.update({k: v for k, v in result.items() if k in base})
    return base


# ── Cover letter generation ────────────────────────────────────────────────

_CL_SYSTEM = (
    "You are an expert cover letter writer. Write a compelling, personalised cover letter "
    "that shows genuine enthusiasm for the role and company. Reference specific details from "
    "the job description. Be concise — 3 short paragraphs max. "
    "Respond ONLY with valid JSON, no markdown."
)

_CL_USER = """Profile:
{profile}

Target Job: {title} at {company}
Job Description:
{description}
Candidate Name: {name}

Respond with JSON:
{{
  "salutation": "Dear Hiring Manager,",
  "paragraphs": [
    "<opening para: why this role + company>",
    "<middle para: key relevant experience>",
    "<closing para: call to action>"
  ],
  "closing": "Sincerely,"
}}"""


def generate_cover_letter_content(
    job: JobListing, profile_text: str, api_key: str, personal: dict
) -> dict:
    result = _call_llm(
        api_key,
        _CL_SYSTEM,
        _CL_USER.format(
            profile=profile_text[:2000],
            title=job.title,
            company=job.company,
            description=job.description[:1500],
            name=personal.get("name", ""),
        ),
    )
    base = {
        "name": personal.get("name", ""),
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "location": personal.get("location", ""),
        "company": job.company,
        "job_title": job.title,
        "salutation": "Dear Hiring Manager,",
        "paragraphs": ["I am writing to express my interest in this position."],
        "closing": "Sincerely,",
    }
    if result:
        base.update({k: v for k, v in result.items() if k in base})
    return base


# ── ATS skill code generation ──────────────────────────────────────────────

_ATS_SYSTEM = (
    "You are a Playwright automation expert. Given an ATS job application form's HTML and a "
    "screenshot (base64), write a Python async function that fills and submits the form. "
    "The function signature MUST be: async def apply(page, job, config)\n"
    "- page: Playwright Page object\n"
    "- job: has .title, .company, .description attributes\n"
    "- config: has .your_full_name, .your_email, .your_phone, .your_location, .cv_path attribute\n"
    "Use standard Playwright selectors. Handle waits. Do not import anything. "
    "Respond ONLY with the raw Python function code, no markdown fences, no explanation."
)

_ATS_USER = """Domain: {domain}

Page HTML (truncated):
{html}

Previous error (if any): {error}

Write the async def apply(page, job, config) function:"""


def generate_ats_skill_code(
    domain: str,
    page_html: str,
    api_key: str,
    error_context: Optional[str] = None,
) -> Optional[str]:
    code = _call_llm_raw(
        api_key,
        _ATS_SYSTEM,
        _ATS_USER.format(
            domain=domain,
            html=page_html[:4000],
            error=error_context or "none",
        ),
    )
    if not code:
        return None
    # Strip markdown fences if the model added them despite instructions
    code = re.sub(r"^```(?:python)?\s*|\s*```$", "", code.strip())
    return code
