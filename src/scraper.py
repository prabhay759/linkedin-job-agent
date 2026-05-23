from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from src.config import Config
from src.models import JobListing

log = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
_GUEST_JOBS_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{}/"

_COOKIES_FILE = Path("data/linkedin_cookies.json")
_PROFILE_CACHE = Path("data/profile_cache.txt")

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-extensions",
    "--disable-infobars",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""

_GUEST_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Browser context (used only for Easy Apply) ────────────────────────────

async def _make_context(pw):
    browser = await pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    await context.add_init_script(_STEALTH_SCRIPT)
    return browser, context


async def _save_cookies(context) -> None:
    cookies = await context.cookies()
    _COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
    log.info("Saved %d LinkedIn cookies", len(cookies))


async def _load_cookies(context) -> bool:
    if not _COOKIES_FILE.exists():
        return False
    try:
        cookies = json.loads(_COOKIES_FILE.read_text())
        await context.add_cookies(cookies)
        log.info("Loaded %d cookies from cache", len(cookies))
        return True
    except Exception as e:
        log.warning("Could not load cookies: %s", e)
        return False


async def _is_logged_in(page) -> bool:
    try:
        await page.goto("https://www.linkedin.com/feed/", wait_until="load", timeout=30_000)
        await page.wait_for_timeout(1500)
        return "/feed" in page.url and "/login" not in page.url
    except Exception:
        return False


async def _login_linkedin(page, context, config: Config) -> bool:
    """Cookie-first login. Used by applicator only — job discovery is login-free."""
    if await _load_cookies(context):
        if await _is_logged_in(page):
            log.info("LinkedIn session restored from cookies")
            return True
        log.info("Cached cookies expired — trying fresh login")

    try:
        await page.goto(LINKEDIN_LOGIN_URL, wait_until="load", timeout=60_000)
        await page.wait_for_selector("#username", timeout=20_000)
        await page.wait_for_timeout(800)
        await page.fill("#username", config.linkedin_email)
        await page.wait_for_timeout(400)
        await page.fill("#password", config.linkedin_password)
        await page.wait_for_timeout(600)
        await page.click('button[type="submit"]')
        await page.wait_for_load_state("load", timeout=30_000)
        await page.wait_for_timeout(2000)

        url = page.url
        if "/feed" in url:
            await _save_cookies(context)
            log.info("LinkedIn login successful")
            return True
        if any(x in url for x in ["/checkpoint", "/check/", "/challenge", "/authwall"]):
            log.error("LinkedIn checkpoint at %s — send your browser cookies via /setcookies", url)
            return False
        if "/login" in url:
            log.error("LinkedIn rejected credentials — check LINKEDIN_EMAIL / LINKEDIN_PASSWORD")
            return False
        nav = await page.query_selector(".global-nav__me, #global-nav")
        if nav:
            await _save_cookies(context)
            return True
        log.error("Unknown post-login state: %s", url)
        return False
    except Exception as e:
        log.error("LinkedIn login failed: %s", e)
        return False


# ── Public profile scraping (httpx — no browser, no checkpoint) ───────────

async def scrape_profile(profile_url: str) -> str:
    """
    Fetch a LinkedIn public profile via httpx (no Playwright = no checkpoint risk).
    Caches result to data/profile_cache.txt for resilience across redeploys.
    Falls back to cached text if LinkedIn blocks the request.
    """
    try:
        async with httpx.AsyncClient(
            headers=_GUEST_HEADERS,
            follow_redirects=False,
            timeout=30,
        ) as client:
            r = await client.get(profile_url)

        # LinkedIn may redirect to authwall — treat as blocked
        if r.status_code in (301, 302):
            location = r.headers.get("location", "")
            if "authwall" in location or "login" in location:
                raise ValueError(f"LinkedIn redirected to {location}")

        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")

        text = _parse_profile_html(r.text)
        if len(text) < 100:
            raise ValueError("Profile HTML returned too little text — likely blocked")

        # Cache on success
        _PROFILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _PROFILE_CACHE.write_text(text)
        log.info("Profile scraped and cached: %d chars", len(text))
        return text

    except Exception as e:
        log.warning("Profile scrape failed (%s) — checking cache", e)
        if _PROFILE_CACHE.exists():
            cached = _PROFILE_CACHE.read_text().strip()
            if cached:
                log.info("Using cached profile text (%d chars)", len(cached))
                return cached
        raise RuntimeError(
            "Could not scrape LinkedIn profile and no cache found. "
            "Use /setprofile in Telegram to paste your profile text manually."
        ) from e


def _parse_profile_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    sections = []

    name_el = soup.find("h1")
    if name_el:
        sections.append(f"Name: {name_el.get_text(strip=True)}")

    headline_el = soup.find("div", {"class": re.compile(r"top-card.*subtitle|pv-text-details__left-panel")})
    if headline_el:
        sections.append(f"Headline: {headline_el.get_text(' ', strip=True)[:200]}")

    about = soup.find("div", {"class": re.compile(r"summary|about")})
    if about:
        sections.append(f"About: {about.get_text(' ', strip=True)[:600]}")

    for section in soup.find_all("section"):
        heading = section.find(["h2", "h3"])
        if heading:
            sections.append(f"{heading.get_text(strip=True)}:\n{section.get_text(' ', strip=True)[:800]}")

    return "\n\n".join(sections) if sections else soup.get_text(" ", strip=True)[:3000]


# ── Public job discovery (no login required) ──────────────────────────────

def _extract_job_id(url: str) -> Optional[str]:
    # New slug format: /jobs/view/software-architect-at-company-4414097827?...
    # Old format:      /jobs/view/4414097827
    path = url.split("?")[0].rstrip("/")
    m = re.search(r"/jobs/view/[^/]*?(\d+)$", path)
    if m:
        return m.group(1)
    m = re.search(r"currentJobId=(\d+)", url)
    return m.group(1) if m else None


def _job_id_from_card(card) -> Optional[str]:
    """Extract job ID from data-entity-urn (most reliable) or href fallback."""
    urn = card.get("data-entity-urn", "")  # e.g. "urn:li:jobPosting:4414097827"
    m = re.search(r":(\d+)$", urn)
    if m:
        return m.group(1)
    # fallback: parse the href
    link = card.find("a", class_=re.compile(r"base-card__full-link"))
    if link:
        return _extract_job_id(link.get("href", ""))
    return None


async def fetch_jobs(config: Config, seen_ids: set) -> List[JobListing]:
    """
    Discover LinkedIn jobs without login.
    Uses LinkedIn's public guest job search API via httpx — no browser, no checkpoint risk.
    """
    jobs: List[JobListing] = []
    seen_in_run: set = set()

    async with httpx.AsyncClient(
        headers=_GUEST_HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:
        first_combo = True
        for keyword in config.job_keywords:
            for location in config.job_locations:
                if len(jobs) >= config.max_jobs_per_scan:
                    break
                # Pause between combos to avoid rate limiting (skip on first)
                if not first_combo:
                    await asyncio.sleep(5 + random.uniform(0, 3))
                first_combo = False

                fetched = await _search_jobs_guest(
                    client, keyword, location,
                    seen_ids | seen_in_run,
                    config.max_jobs_per_scan - len(jobs),
                )
                for j in fetched:
                    seen_in_run.add(j.id)
                    jobs.append(j)
                log.info("Found %d new jobs for '%s' in '%s'", len(fetched), keyword, location)

    return jobs


async def _polite_get(client: httpx.AsyncClient, url: str, **kwargs) -> Optional[httpx.Response]:
    """GET with 429-aware retry and jittered delays to avoid rate limiting."""
    for attempt in range(1, 4):
        try:
            r = await client.get(url, **kwargs)
            if r.status_code == 429:
                wait = 15 * attempt + random.uniform(0, 5)
                log.warning("LinkedIn 429 rate-limit — waiting %.0fs (attempt %d/3)", wait, attempt)
                await asyncio.sleep(wait)
                continue
            return r
        except Exception as e:
            log.warning("Request error (attempt %d/3): %s", attempt, e)
            await asyncio.sleep(5 * attempt)
    return None


async def _search_jobs_guest(
    client: httpx.AsyncClient,
    keyword: str,
    location: str,
    seen_ids: set,
    max_results: int,
) -> List[JobListing]:
    jobs: List[JobListing] = []
    page_size = 25

    for start in range(0, max_results + page_size, page_size):
        if len(jobs) >= max_results:
            break

        params = {
            "keywords": keyword,
            "start": start,
            "f_TPR": "r86400",
            "count": page_size,
        }
        if location:
            params["location"] = location

        r = await _polite_get(client, _GUEST_JOBS_API, params=params)
        if r is None or r.status_code != 200:
            log.warning("LinkedIn guest API failed at start=%d for '%s'", start, keyword)
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.find_all("div", class_=re.compile(r"base-card"))
        if not cards:
            log.info("No more results at start=%d for '%s'", start, keyword)
            break

        page_new = 0
        for card in cards:
            if len(jobs) >= max_results:
                break

            job_id = _job_id_from_card(card)
            if not job_id or job_id in seen_ids:
                continue

            title = _text(card.find(class_=re.compile(r"base-search-card__title")))
            company = _text(card.find(class_=re.compile(r"base-search-card__subtitle")))
            loc = _text(card.find(class_=re.compile(r"job-search-card__location"))) or location

            # Polite delay before each detail fetch (2-4s with jitter)
            await asyncio.sleep(2 + random.uniform(0, 2))

            detail = await _fetch_job_detail_guest(client, job_id)  # always a dict

            jobs.append(JobListing(
                id=job_id,
                title=title or "Unknown Title",
                company=company or "Unknown Company",
                location=loc,
                url=_JOB_VIEW_URL.format(job_id),
                apply_url=detail.get("apply_url"),
                description=detail.get("description", ""),
                is_easy_apply=detail.get("is_easy_apply", False),
                scraped_at=datetime.now(timezone.utc).isoformat(),
            ))
            page_new += 1

        log.info("Page start=%d: %d new jobs for '%s'", start, page_new, keyword)

        # Stop paginating if this page was mostly already-seen
        if page_new == 0:
            break

        # Delay between pages
        if len(jobs) < max_results:
            await asyncio.sleep(3 + random.uniform(0, 2))

    return jobs


async def _fetch_job_detail_guest(
    client: httpx.AsyncClient, job_id: str
) -> dict:
    """Always returns a dict — never None. Jobs are added even if detail fetch fails."""
    empty = {"description": "", "is_easy_apply": False, "apply_url": None}
    try:
        r = await _polite_get(client, _JOB_VIEW_URL.format(job_id), timeout=20)
        if r is None or r.status_code != 200:
            log.debug("Detail fetch failed for job %s (status %s)", job_id, r.status_code if r else "None")
            return empty

        soup = BeautifulSoup(r.text, "html.parser")

        desc_div = soup.find("div", class_=re.compile(r"show-more-less-html__markup|description__text"))
        description = desc_div.get_text(" ", strip=True)[:3000] if desc_div else ""

        is_easy_apply = bool(
            soup.find("span", string=re.compile(r"Easy Apply", re.I))
            or soup.find(class_=re.compile(r"easy-apply", re.I))
        )

        apply_url: Optional[str] = None
        if not is_easy_apply:
            btn = soup.find("a", class_=re.compile(r"apply-button|sign-up-modal"))
            if btn:
                apply_url = btn.get("href")

        return {"description": description, "is_easy_apply": is_easy_apply, "apply_url": apply_url}
    except Exception as e:
        log.debug("Detail fetch error for job %s: %s", job_id, e)
        return empty


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""
