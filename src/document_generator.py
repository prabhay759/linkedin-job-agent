from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path("templates")
OUTPUT_DIR = Path("data/pdfs")


def _ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _render_html(template_name: str, context: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
    template = env.get_template(template_name)
    return template.render(**context)


async def render_pdf(template_name: str, context: dict, output_path: Path) -> Path:
    html = _render_html(template_name, context)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        try:
            await page.set_content(html, wait_until="networkidle")
            await page.pdf(
                path=str(output_path),
                format="A4",
                margin={"top": "20mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
                print_background=True,
            )
        finally:
            await browser.close()

    log.info("PDF written: %s", output_path)
    return output_path


async def generate_cv_pdf(cv_data: dict, job_id: str) -> Path:
    _ensure_output_dir()
    output = OUTPUT_DIR / f"cv_{job_id}.pdf"
    return await render_pdf("cv.html.j2", cv_data, output)


async def generate_cover_letter_pdf(cl_data: dict, job_id: str) -> Path:
    _ensure_output_dir()
    output = OUTPUT_DIR / f"cl_{job_id}.pdf"
    return await render_pdf("cover_letter.html.j2", cl_data, output)
