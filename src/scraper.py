from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Page, async_playwright

from src.config import Config
from src.models import JobListing

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


def _build_search_url(keyword: str, location: str, start: int = 0) -> str:
    params = {
        "keywords": keyword,
        "location": location,
        "f_TPR": "r86400",  # posted in last 24 hours
        "start": start,
    }
    return f"{LINKEDIN_JOBS_SEARCH}?{urlencode(params)}"


async def _login_linkedin(page: Page, config: Config) -> bool:
    try:
        await page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.fill(_SEL["email"], config.linkedin_email)
        await page.fill(_SEL["password"], config.linkedin_password)
        await page.click(_SEL["submit"])
        await page.wait_for_url("**/feed/**", timeout=20_000)
        log.info("LinkedIn login successful")
        return True
    except Exception as e:
        log.error("LinkedIn login failed: %s", e)
        return False


async def scrape_profile(profile_url: str) -> str:
    """Scrape public LinkedIn profile, return plain text for LLM."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
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
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            logged_in = await _login_linkedin(page, config)
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
