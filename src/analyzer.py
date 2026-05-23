from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

import httpx

from src.models import JobListing

log = logging.getLogger(__name__)

TOGETHER_API = "https://api.together.xyz/v1/chat/completions"
LLAMA_MODEL = "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"
_TIMEOUT = 45
_TEMPERATURE = 0.2


def _call_llm(api_key: str, system: str, user: str) -> Optional[dict]:
    payload = {
        "model": LLAMA_MODEL,
        "temperature": _TEMPERATURE,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        r = httpx.post(
            TOGETHER_API,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.error("LLM returned invalid JSON: %s", e)
        return None
    except Exception as e:
        log.error("LLM call failed: %s", e)
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
    payload = {
        "model": LLAMA_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": _ATS_SYSTEM},
            {
                "role": "user",
                "content": _ATS_USER.format(
                    domain=domain,
                    html=page_html[:4000],
                    error=error_context or "none",
                ),
            },
        ],
    }
    try:
        r = httpx.post(
            TOGETHER_API,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        code = r.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if code.startswith("```"):
            lines = code.splitlines()
            code = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return code
    except Exception as e:
        log.error("ATS skill code generation failed: %s", e)
        return None
