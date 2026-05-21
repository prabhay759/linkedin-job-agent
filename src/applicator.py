from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from src.config import Config
from src.models import Application, JobListing
from src.skill_manager import SkillManager

log = logging.getLogger(__name__)

# Easy Apply modal selectors
_EA = {
    "easy_apply_btn": 'button[aria-label*="Easy Apply"], .jobs-apply-button',
    "modal": ".jobs-easy-apply-modal, .artdeco-modal",
    "next_btn": 'button[aria-label="Continue to next step"], button[aria-label*="Next"]',
    "review_btn": 'button[aria-label="Review your application"]',
    "submit_btn": 'button[aria-label="Submit application"], button[aria-label*="Submit"]',
    "phone_input": 'input[name*="phone"], input[id*="phone"]',
    "file_upload": 'input[type="file"]',
    "close_btn": 'button[aria-label="Dismiss"]',
}


class Applicator:
    def __init__(self, config: Config, skill_manager: SkillManager) -> None:
        self._config = config
        self._skills = skill_manager

    async def apply(self, app: Application, job: JobListing) -> bool:
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
                # Log in
                from src.scraper import _login_linkedin
                logged_in = await _login_linkedin(page, self._config)
                if not logged_in:
                    app.error = "LinkedIn login failed"
                    return False

                if job.is_easy_apply:
                    return await self._apply_easy_apply(page, job, app)
                elif job.apply_url:
                    return await self._apply_external(page, job, app)
                else:
                    # Navigate to job and try Easy Apply
                    await page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(2000)
                    easy_btn = await page.query_selector(_EA["easy_apply_btn"])
                    if easy_btn:
                        return await self._apply_easy_apply(page, job, app)
                    app.error = "No apply button found"
                    return False
            except Exception as e:
                app.error = str(e)
                log.error("Apply failed for %s: %s", job.title, e)
                return False
            finally:
                await browser.close()

    async def _apply_easy_apply(self, page, job: JobListing, app: Application) -> bool:
        try:
            await page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)

            easy_btn = await page.query_selector(_EA["easy_apply_btn"])
            if not easy_btn:
                app.error = "Easy Apply button not found"
                return False

            await easy_btn.click()
            await page.wait_for_selector(_EA["modal"], timeout=10_000)
            await page.wait_for_timeout(1000)

            # Multi-step wizard loop
            max_steps = 10
            for _ in range(max_steps):
                # Upload CV if file upload present
                file_input = await page.query_selector(_EA["file_upload"])
                if file_input and app.cv_path and Path(app.cv_path).exists():
                    await file_input.set_input_files(app.cv_path)
                    await page.wait_for_timeout(1000)

                # Fill phone if present
                phone_input = await page.query_selector(_EA["phone_input"])
                if phone_input and self._config.your_phone:
                    val = await phone_input.input_value()
                    if not val:
                        await phone_input.fill(self._config.your_phone)

                # Answer Yes/No radio questions heuristically (choose "Yes" for positive questions)
                await self._answer_screening_questions(page)

                # Try Submit first, then Review, then Next
                submitted = await self._try_click(page, _EA["submit_btn"])
                if submitted:
                    await page.wait_for_timeout(2000)
                    log.info("Easy Apply submitted for %s @ %s", job.title, job.company)
                    return True

                reviewed = await self._try_click(page, _EA["review_btn"])
                if reviewed:
                    await page.wait_for_timeout(1500)
                    continue

                nexted = await self._try_click(page, _EA["next_btn"])
                if nexted:
                    await page.wait_for_timeout(1500)
                    continue

                # Nothing to click — stuck
                break

            app.error = "Easy Apply wizard did not reach submission"
            return False

        except Exception as e:
            app.error = f"Easy Apply error: {e}"
            log.error("Easy Apply failed: %s", e)
            return False

    async def _answer_screening_questions(self, page) -> None:
        """Heuristically answer Yes/No screening questions — pick 'Yes' for positive questions."""
        try:
            # Find all radio groups
            fieldsets = await page.query_selector_all("fieldset")
            for fieldset in fieldsets:
                label_el = await fieldset.query_selector("legend, label")
                label_text = (await label_el.inner_text()).lower() if label_el else ""
                # Select Yes for authorization/experience questions, No for negative ones
                positive_keywords = ["authorized", "eligible", "right to work", "experience", "years"]
                pick_yes = any(kw in label_text for kw in positive_keywords)
                radio_value = "Yes" if pick_yes else None
                if radio_value:
                    radio = await fieldset.query_selector(f'input[value="{radio_value}"]')
                    if radio:
                        await radio.click()
        except Exception:
            pass  # Non-fatal — screening questions are optional

    async def _try_click(self, page, selector: str) -> bool:
        try:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible() and await btn.is_enabled():
                await btn.click()
                return True
        except Exception:
            pass
        return False

    async def _apply_external(self, page, job: JobListing, app: Application) -> bool:
        if not job.apply_url:
            app.error = "No external apply URL"
            return False

        parsed = urlparse(job.apply_url)
        domain = parsed.netloc.lstrip("www.")

        try:
            await page.goto(job.apply_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)
            page_html = await page.content()
        except Exception as e:
            app.error = f"Could not load external ATS page: {e}"
            return False

        # Use existing skill if healthy, else learn a new one
        if self._skills.get_skill(domain) and not self._skills.needs_regen(domain):
            success = await self._skills.execute_skill(domain, page, job, self._config)
        else:
            success = await self._skills.learn_and_execute(
                domain, page, job, self._config, page_html
            )

        if not success:
            app.error = f"External ATS application failed for {domain}"
        return success
