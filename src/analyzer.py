from __future__ import annotations

import json
import logging
import re
import time
from typing import List, Optional, Tuple

import httpx

from src.models import JobListing

log = logging.getLogger(__name__)

OPENROUTER_API = "https://openrouter.ai/api/v1"
_TIMEOUT = 60
_TEMPERATURE = 0.2

# Curated list of free models known to work well — tried in order on failure
_CANDIDATE_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-3.1-70b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-7b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
]

_selected_model: Optional[str] = None

_HEADERS_TMPL = {
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/prabhay759/linkedin-job-agent",
    "X-Title": "LinkedIn Job Agent",
}


def _auth_headers(api_key: str) -> dict:
    return {**_HEADERS_TMPL, "Authorization": f"Bearer {api_key}"}


# ── Model selection ────────────────────────────────────────────────────────

def detect_free_model(api_key: str) -> str:
    """
    Try the curated candidate list in order, return the first that responds.
    Falls back to the last candidate if nothing works.
    Result cached for the process lifetime.
    """
    global _selected_model
    if _selected_model:
        return _selected_model

    if not api_key:
        log.error("OPENROUTER_API_KEY is not set — LLM calls will fail")
        _selected_model = _CANDIDATE_MODELS[0]
        return _selected_model

    # Quick probe: ask the models list endpoint for free models
    try:
        r = httpx.get(
            f"{OPENROUTER_API}/models",
            headers=_auth_headers(api_key),
            timeout=15,
        )
        if r.status_code == 401:
            log.error("OpenRouter: invalid API key (401). Check OPENROUTER_API_KEY in Railway.")
            _selected_model = _CANDIDATE_MODELS[0]
            return _selected_model

        models_by_id = {m["id"]: m for m in r.json().get("data", [])}

        # Pick the first candidate that appears in the model list
        for candidate in _CANDIDATE_MODELS:
            if candidate in models_by_id:
                _selected_model = candidate
                ctx = models_by_id[candidate].get("context_length", "?")
                log.info("Selected free model: %s (context: %s tokens)", candidate, ctx)
                return _selected_model
    except Exception as e:
        log.warning("Could not probe OpenRouter models: %s — using first candidate", e)

    _selected_model = _CANDIDATE_MODELS[0]
    log.info("Using default model: %s", _selected_model)
    return _selected_model


def _try_next_model() -> None:
    """Cycle _selected_model to the next candidate after a failure."""
    global _selected_model
    try:
        idx = _CANDIDATE_MODELS.index(_selected_model)
        next_idx = (idx + 1) % len(_CANDIDATE_MODELS)
    except ValueError:
        next_idx = 0
    _selected_model = _CANDIDATE_MODELS[next_idx]
    log.info("Switched to model: %s", _selected_model)


# ── Core HTTP call ─────────────────────────────────────────────────────────

def _post(api_key: str, payload: dict, retries: int = 3) -> Optional[dict]:
    """
    POST to OpenRouter with retry + model cycling.
    Returns the full response JSON or None on permanent failure.
    """
    for attempt in range(1, retries + 1):
        model = detect_free_model(api_key)
        payload["model"] = model
        try:
            r = httpx.post(
                f"{OPENROUTER_API}/chat/completions",
                headers=_auth_headers(api_key),
                json=payload,
                timeout=_TIMEOUT,
            )

            if r.status_code == 401:
                log.error("OpenRouter 401 — invalid API key. Set OPENROUTER_API_KEY in Railway.")
                return None  # no point retrying

            if r.status_code == 429:
                wait = 10 * attempt
                log.warning("OpenRouter rate-limited (429) — waiting %ds", wait)
                time.sleep(wait)
                continue

            if r.status_code in (400, 422):
                # Model may not support this feature (e.g. json_object mode)
                log.warning(
                    "OpenRouter %d with model %s: %s — trying next model",
                    r.status_code, model, r.text[:200],
                )
                _try_next_model()
                continue

            if not r.is_success:
                log.warning(
                    "OpenRouter %d on attempt %d/%d: %s",
                    r.status_code, attempt, retries, r.text[:300],
                )
                time.sleep(3 * attempt)
                _try_next_model()
                continue

            return r.json()

        except httpx.TimeoutException:
            log.warning("OpenRouter timeout on attempt %d/%d (model: %s)", attempt, retries, model)
            time.sleep(5)
        except Exception as e:
            log.warning("OpenRouter request error attempt %d/%d: %s", attempt, retries, e)
            time.sleep(3)

    log.error("OpenRouter: all %d attempts failed for model %s", retries, _selected_model)
    return None


def _extract_json(text: str) -> Optional[dict]:
    """Extract JSON from model response — handles markdown fences and prose wrappers."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to find a JSON object anywhere in the text
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    log.error("Could not extract JSON from LLM response: %.200s", text)
    return None


# ── Public API ─────────────────────────────────────────────────────────────

def _call_llm(api_key: str, system: str, user: str) -> Optional[dict]:
    """Call LLM expecting JSON back. Tries with json_object mode, falls back without."""
    payload = {
        "temperature": _TEMPERATURE,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = _post(api_key, payload)
    if not resp:
        # Retry without json_object mode (some models don't support it)
        payload.pop("response_format", None)
        resp = _post(api_key, payload)
    if not resp:
        return None

    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _extract_json(content)


def _call_llm_raw(api_key: str, system: str, user: str) -> Optional[str]:
    """Call LLM expecting plain text back (used for code generation)."""
    payload = {
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = _post(api_key, payload)
    if not resp:
        return None
    return resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or None


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
