from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

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


# ── Browser context (used only for applying) ──────────────────────────────

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
        log.info("Cached cookies expired — doing fresh login")

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


# ── Public profile scraping ────────────────────────────────────────────────

async def scrape_profile(profile_url: str) -> str:
    """Scrape a public LinkedIn profile page, return plain text for LLM prompts."""
    async with async_playwright() as pw:
        browser, context = await _make_context(pw)
        page = await context.new_page()
        try:
            await page.goto(profile_url, wait_until="load", timeout=60_000)
            await page.wait_for_timeout(2000)
            html = await page.content()
        finally:
            await browser.close()

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
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"currentJobId=(\d+)", url)
    return m.group(1) if m else None


async def fetch_jobs(config: Config, seen_ids: set) -> List[JobListing]:
    """
    Discover LinkedIn jobs without requiring login.
    Uses LinkedIn's public guest job API via httpx — no browser, no checkpoint risk.
    """
    jobs: List[JobListing] = []
    seen_in_run: set = set()

    async with httpx.AsyncClient(
        headers=_GUEST_HEADERS,
        follow_redirects=True,
        timeout=30,
    ) as client:
        for keyword in config.job_keywords:
            for location in config.job_locations:
                if len(jobs) >= config.max_jobs_per_scan:
                    break
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


async def _search_jobs_guest(
    client: httpx.AsyncClient,
    keyword: str,
    location: str,
    seen_ids: set,
    max_results: int,
) -> List[JobListing]:
    try:
        r = await client.get(
            _GUEST_JOBS_API,
            params={
                "keywords": keyword,
                "location": location,
                "start": 0,
                "f_TPR": "r86400",
                "count": min(max_results, 25),
            },
        )
        if r.status_code != 200:
            log.warning("LinkedIn guest API %d for '%s'/'%s'", r.status_code, keyword, location)
            return []
    except Exception as e:
        log.warning("LinkedIn guest API request failed: %s", e)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    jobs: List[JobListing] = []

    for card in soup.find_all("div", class_=re.compile(r"base-card")):
        link_el = card.find("a", class_=re.compile(r"base-card__full-link"))
        if not link_el:
            continue
        href = link_el.get("href", "")
        job_id = _extract_job_id(href)
        if not job_id or job_id in seen_ids:
            continue

        title = _text(card.find(class_=re.compile(r"base-search-card__title")))
        company = _text(card.find(class_=re.compile(r"base-search-card__subtitle")))
        loc = _text(card.find(class_=re.compile(r"job-search-card__location"))) or location

        detail = await _fetch_job_detail_guest(client, job_id)
        if detail is None:
            continue

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

        await asyncio.sleep(0.8)
        if len(jobs) >= max_results:
            break

    return jobs


async def _fetch_job_detail_guest(
    client: httpx.AsyncClient, job_id: str
) -> Optional[dict]:
    try:
        r = await client.get(_JOB_VIEW_URL.format(job_id), timeout=20)
        if r.status_code != 200:
            return None
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
        log.warning("Failed to fetch job detail for %s: %s", job_id, e)
        return None


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


log = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_JOBS_SEARCH = "https://www.linkedin.com/jobs/search/"

# CSS selectors — fragile against LinkedIn DOM changes, but best available
_SEL = {
    "email": "#username",
    "password": "#password",
    "submit": 'button[type="submit"]',
    "job_cards": ".jobs-search__results-list li, .scaffold-layout__list-item",
    "job_link": "a.job-card-list__title--link, a.job-card-container__link",
    "job_title": ".job-card-list__title--link, .job-card-container__link",
    "job_company": ".job-card-container__primary-description, .job-card-container__company-name",
    "job_location": ".job-card-container__metadata-item",
    "easy_apply_btn": 'button.jobs-apply-button[aria-label*="Easy Apply"], .jobs-apply-button--top-card',
    "job_description": ".jobs-description__content .show-more-less-html__markup, .jobs-description-content__text",
    "apply_btn": ".jobs-apply-button",
}


def _extract_job_id(url: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"currentJobId=(\d+)", url)
    if m:
        return m.group(1)
    # fallback: hash the URL
    return str(abs(hash(url)))


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

# Injected into every page to hide webdriver fingerprint
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""


_COOKIES_FILE = Path("data/linkedin_cookies.json")


async def _make_context(pw):
    """Return a stealth browser context that mimics a real Chrome installation."""
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
    """Quick check: visit LinkedIn and see if we land on the feed without login."""
    try:
        await page.goto("https://www.linkedin.com/feed/", wait_until="load", timeout=30_000)
        await page.wait_for_timeout(1500)
        return "/feed" in page.url and "/login" not in page.url
    except Exception:
        return False


def _build_search_url(keyword: str, location: str, start: int = 0) -> str:
    params = {
        "keywords": keyword,
        "location": location,
        "f_TPR": "r86400",  # posted in last 24 hours
        "start": start,
    }
    return f"{LINKEDIN_JOBS_SEARCH}?{urlencode(params)}"


async def _login_linkedin(page, context, config: Config) -> bool:
    """
    Login flow with cookie caching:
    1. Try saved cookies first (avoids login page entirely)
    2. Fall back to email/password login
    3. After submit, handle feed / checkpoint / verification states
    4. On success, save cookies for next run
    """
    # Try cached session first
    if await _load_cookies(context):
        if await _is_logged_in(page):
            log.info("LinkedIn session restored from cookies")
            return True
        log.info("Cached cookies expired — doing fresh login")

    try:
        await page.goto(LINKEDIN_LOGIN_URL, wait_until="load", timeout=60_000)
        await page.wait_for_selector(_SEL["email"], timeout=20_000)
        await page.wait_for_timeout(800)
        await page.fill(_SEL["email"], config.linkedin_email)
        await page.wait_for_timeout(400)
        await page.fill(_SEL["password"], config.linkedin_password)
        await page.wait_for_timeout(600)
        await page.click(_SEL["submit"])

        # Wait for any navigation to settle (not just /feed)
        await page.wait_for_load_state("load", timeout=30_000)
        await page.wait_for_timeout(2000)

        url = page.url

        if "/feed" in url:
            log.info("LinkedIn login successful")
            await _save_cookies(context)
            return True

        if any(x in url for x in ["/checkpoint", "/check/", "/challenge", "/uas/", "/authwall"]):
            log.error(
                "LinkedIn requires manual verification (URL: %s). "
                "Please log in manually via a browser, export cookies to "
                "data/linkedin_cookies.json and redeploy.",
                url,
            )
            return False

        if "/login" in url:
            log.error("LinkedIn credentials rejected — check LINKEDIN_EMAIL and LINKEDIN_PASSWORD")
            return False

        # Unknown page — check if the nav bar is visible (means we're logged in)
        nav = await page.query_selector(".global-nav__me, #global-nav")
        if nav:
            log.info("LinkedIn login successful (URL: %s)", url)
            await _save_cookies(context)
            return True

        log.error("LinkedIn login: unknown post-login state at %s", url)
        return False

    except Exception as e:
        log.error("LinkedIn login failed: %s", e)
        return False


async def scrape_profile(profile_url: str) -> str:
    """Scrape public LinkedIn profile, return plain text for LLM."""
    async with async_playwright() as pw:
        browser, context = await _make_context(pw)
        page = await context.new_page()
        try:
            await page.goto(profile_url, wait_until="load", timeout=60_000)
            await page.wait_for_timeout(2000)
            html = await page.content()
        finally:
            await browser.close()

    soup = BeautifulSoup(html, "html.parser")
    # Remove script/style noise
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    sections = []

    # Name / headline
    name_el = soup.find("h1")
    if name_el:
        sections.append(f"Name: {name_el.get_text(strip=True)}")

    headline_el = soup.find("div", {"class": re.compile(r"top-card.*subtitle|pv-text-details__left-panel")})
    if headline_el:
        sections.append(f"Headline: {headline_el.get_text(' ', strip=True)[:200]}")

    # About
    about = soup.find("div", {"class": re.compile(r"summary|about")})
    if about:
        sections.append(f"About: {about.get_text(' ', strip=True)[:600]}")

    # Experience / skills / education sections — grab all meaningful text blocks
    for section in soup.find_all("section"):
        heading = section.find(["h2", "h3"])
        if heading:
            heading_text = heading.get_text(strip=True)
            content = section.get_text(" ", strip=True)[:800]
            sections.append(f"{heading_text}:\n{content}")

    return "\n\n".join(sections) if sections else soup.get_text(" ", strip=True)[:3000]


async def fetch_jobs(config: Config, seen_ids: set) -> List[JobListing]:
    """Log into LinkedIn, search jobs for all keyword×location combos, return new JobListings."""
    jobs: List[JobListing] = []
    seen_in_this_run: set = set()

    async with async_playwright() as pw:
        browser, context = await _make_context(pw)
        page = await context.new_page()

        try:
            logged_in = await _login_linkedin(page, context, config)
            if not logged_in:
                log.error("Could not log into LinkedIn — skipping job fetch")
                return []

            for keyword in config.job_keywords:
                for location in config.job_locations:
                    if len(jobs) >= config.max_jobs_per_scan:
                        break
                    fetched = await _search_jobs(
                        page,
                        keyword,
                        location,
                        seen_ids | seen_in_this_run,
                        max_results=config.max_jobs_per_scan - len(jobs),
                    )
                    for j in fetched:
                        seen_in_this_run.add(j.id)
                        jobs.append(j)
                    log.info("Found %d new jobs for '%s' in '%s'", len(fetched), keyword, location)
        finally:
            await browser.close()

    return jobs


async def _search_jobs(
    page: Page,
    keyword: str,
    location: str,
    seen_ids: set,
    max_results: int,
) -> List[JobListing]:
    jobs: List[JobListing] = []
    url = _build_search_url(keyword, location)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("Job search page load failed for %s/%s: %s", keyword, location, e)
        return []

    # Collect job card links
    links = await page.query_selector_all(_SEL["job_link"])
    job_urls = []
    for link in links:
        href = await link.get_attribute("href")
        if href:
            # Normalise to full URL
            if href.startswith("/"):
                href = "https://www.linkedin.com" + href
            job_id = _extract_job_id(href)
            if job_id not in seen_ids and job_id not in {_extract_job_id(u) for u in job_urls}:
                job_urls.append(href)

    for job_url in job_urls[:max_results]:
        job_id = _extract_job_id(job_url)
        if job_id in seen_ids:
            continue
        job = await _fetch_job_detail(page, job_url, job_id)
        if job:
            jobs.append(job)
        await asyncio.sleep(1.5)  # polite delay

    return jobs


async def _fetch_job_detail(page: Page, job_url: str, job_id: str) -> Optional[JobListing]:
    try:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1500)

        title_el = await page.query_selector("h1.job-details-jobs-unified-top-card__job-title, h1.t-24")
        title = (await title_el.inner_text()).strip() if title_el else "Unknown Title"

        company_el = await page.query_selector(
            ".job-details-jobs-unified-top-card__company-name a, "
            ".jobs-unified-top-card__company-name a"
        )
        company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

        location_el = await page.query_selector(
            ".job-details-jobs-unified-top-card__bullet, "
            ".jobs-unified-top-card__bullet"
        )
        location = (await location_el.inner_text()).strip() if location_el else "Unknown Location"

        # Check Easy Apply
        easy_apply_btn = await page.query_selector(_SEL["easy_apply_btn"])
        is_easy_apply = easy_apply_btn is not None

        # External apply URL (if not easy apply)
        apply_url: Optional[str] = None
        if not is_easy_apply:
            ext_btn = await page.query_selector("a.apply-button--link, a[data-tracking-control-name*='external']")
            if ext_btn:
                apply_url = await ext_btn.get_attribute("href")

        # Description
        desc_el = await page.query_selector(_SEL["job_description"])
        description = (await desc_el.inner_text()).strip() if desc_el else ""

        if not description:
            # Fallback: scrape visible text from description area
            desc_html = await page.content()
            soup = BeautifulSoup(desc_html, "html.parser")
            desc_div = soup.find("div", {"class": re.compile(r"description|show-more-less")})
            description = desc_div.get_text(" ", strip=True)[:3000] if desc_div else ""

        return JobListing(
            id=job_id,
            title=title,
            company=company,
            location=location,
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            apply_url=apply_url,
            description=description[:3000],
            is_easy_apply=is_easy_apply,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        log.warning("Failed to fetch job detail for %s: %s", job_url, e)
        return None
