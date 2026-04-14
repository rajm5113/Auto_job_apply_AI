import sqlite3
import uuid
import asyncio
from datetime import datetime
from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn
from browser.human_delay import delay
from browser.adaptive_locator import AdaptiveLocator
from utils.llm_client import LLMClient
from graph.state import AgentState
from utils.decorators import resilient_node
import config

console = Console()

class ScraperAgent:
    def __init__(self, browser_manager, logger=None):
        self.bm = browser_manager
        self.logger = logger
        self.llm = LLMClient(role="locator")
        
        # Pre-load all historically seen URLs from the DB so we don't count old jobs towards our quota
        self.seen_urls = set()
        try:
            conn = sqlite3.connect(config.DB_PATH)
            rows = conn.execute("SELECT job_url FROM jobs").fetchall()
            self.seen_urls = {r[0] for r in rows}
            conn.close()
        except sqlite3.OperationalError:
            pass

    async def scrape(self, domains: list, city: str,
                     job_type_code: str = "", max_per_domain: int = 50) -> list:
        all_jobs = []

        for domain in domains:
            if self.logger:
                self.logger.info("scraper", f"Scraping domain: {domain} in {city}")

            jobs = await self._scrape_domain(domain, city, job_type_code, max_per_domain)
            all_jobs.extend(jobs)

            if self.logger:
                self.logger.info("scraper", f"Domain '{domain}' — {len(jobs)} jobs found")

            await delay(3000, 6000)

        return all_jobs

    async def _scrape_domain(self, domain: str, city: str,
                             job_type_code: str, max_jobs: int) -> list:
        from urllib.parse import quote

        # The user provides the exact search phrase they want in the CLI.
        # We will use exactly what they provide, combined with the UI filters (f_AL, f_JT).
        search_phrase = domain

        # LinkedIn uses miles in the URL: 50 miles = 80 km
        url = (
            f"https://www.linkedin.com/jobs/search/"
            f"?keywords={quote(search_phrase)}&location={quote(city)}&f_AL=true&distance=50"
        )
        if job_type_code:
            url += f"&f_JT={job_type_code}"

        page = self.bm.page
        locator = AdaptiveLocator(page, self.llm)

        # First, mimic human behavior by navigating directly to the Jobs hub.
        # This resolves session state and can drastically improve LinkedIn's internal search algorithm results.
        if self.logger:
            self.logger.info("scraper", "Navigating to Jobs tab to establish session context...")
        await page.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded")
        await delay(2500, 4000)

        # Then execute the specific search query with all applied filters
        await page.goto(url, wait_until="domcontentloaded")
        await delay(3000, 5000)

        # Check for CAPTCHA or verification wall
        if await self._is_blocked(page):
            return []

        # Scroll to trigger lazy loading
        await self._scroll_to_load(page)

        # Try several known LinkedIn card selectors in sequence — no LLM needed here
        job_cards = []
        card_selectors = [
            "li.jobs-search-results__list-item",
            "div.job-search-card",
            "li.occludable-update",
            "div.jobs-search__results-list > ul > li",
            "ul.jobs-search__results-list li",
        ]
        for sel in card_selectors:
            job_cards = await page.query_selector_all(sel)
            if job_cards:
                console.print(f"[dim]  Found {len(job_cards)} cards with selector: {sel}[/dim]")
                break

        # Last resort — ask adaptive locator
        if not job_cards:
            card_selector = await locator.get_selector(
                action_id="job_search_card",
                default_selector="li.jobs-search-results__list-item",
                hint_description="The container element holding an individual job search result."
            )
            job_cards = await page.query_selector_all(card_selector)

        if self.logger:
            self.logger.info("scraper", f"Found {len(job_cards)} job cards for '{domain}'")

        jobs = []

        with Progress(
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task(f"Scraping {domain}", total=min(len(job_cards), max_jobs))

            for card in job_cards:
                if len(jobs) >= max_jobs:
                    break
                try:
                    job = await self._extract_job(page, card)
                    if job and job["job_url"] not in self.seen_urls:
                        self.seen_urls.add(job["job_url"])
                        jobs.append(job)
                        self._insert_job(job)
                        progress.advance(task)
                    
                    # We still wait so we don't look like a bot
                    await delay(1200, 3000)
                except Exception as e:
                    if self.logger:
                        self.logger.warn("scraper", f"Failed to extract a job card: {e}")
                    continue

        return jobs

    async def _extract_job(self, page, card) -> dict | None:
        # --- Check if already applied ---
        try:
            card_text = await card.inner_text()
            if "Applied" in card_text or "Applied " in card_text:
                return None
        except Exception:
            pass

        # --- Link (job URL) ---
        link = None
        for sel in [
            "a.job-card-list__title",
            "a.job-card-container__link",
            "a[data-tracking-control-name='public_jobs_jserp-result_search-card']",
            "a[href*='/jobs/view/']",
            "a[href*='linkedin.com/jobs']",
        ]:
            link = await card.query_selector(sel)
            if link:
                break

        if not link:
            return None

        href = await link.get_attribute("href")
        if not href:
            return None

        # Normalize to absolute URL — LinkedIn often returns relative paths
        if href.startswith("/"):
            href = "https://www.linkedin.com" + href
        job_url = href.split("?")[0].strip()

        # --- Title ---
        title_el = None
        for sel in [
            "a.job-card-list__title strong",
            "a.job-card-list__title",
            "span.job-card-list__title",
            "h3.job-card-list__title",
            "h3",
        ]:
            title_el = await card.query_selector(sel)
            if title_el:
                break

        # --- Company ---
        company_el = None
        for sel in [
            "span.job-card-container__primary-description",
            "div.artdeco-entity-lockup__subtitle span",
            "span.job-card-container__company-name",
            "a.job-card-container__company-name",
            ".job-search-card__company-name",
        ]:
            company_el = await card.query_selector(sel)
            if company_el:
                break

        # --- Location ---
        location_el = None
        for sel in [
            "li.job-card-container__metadata-item",
            "span.job-card-container__metadata-item",
            ".job-search-card__location",
            "ul.job-card-container__metadata-wrapper li",
        ]:
            location_el = await card.query_selector(sel)
            if location_el:
                break

        # Title — prefer a dedicated element, fall back to the link text itself
        job_title = "Unknown"
        if title_el:
            t = (await title_el.inner_text()).strip()
            if t:
                job_title = t
        if job_title == "Unknown":
            # Try aria-label on the link (LinkedIn often sets this)
            aria = await link.get_attribute("aria-label")
            if aria:
                job_title = aria.strip()
        if job_title == "Unknown":
            # Last resort: raw inner text of the link
            t = (await link.inner_text()).strip()
            if t:
                job_title = t

        company  = (await company_el.inner_text()).strip() if company_el else "Unknown"
        location = (await location_el.inner_text()).strip() if location_el else "Unknown"

        # Click card to load description in detail panel
        try:
            await card.click()
            await delay(1500, 2500)
        except Exception:
            pass

        description = ""
        try:
            for sel in [
                "div.jobs-description__content",
                "div.job-view-layout",
                "article.jobs-description__container",
                "div#job-details",
            ]:
                desc_el = await page.query_selector(sel)
                if desc_el:
                    description = (await desc_el.inner_text()).strip()
                    break
        except Exception:
            pass

        return {
            "id":          str(uuid.uuid4()),
            "job_title":   job_title,
            "company":     company,
            "location":    location,
            "job_url":     job_url,
            "description": description,
            "easy_apply":  1,
            "status":      "scraped",
            "scraped_at":  datetime.now().isoformat()
        }

    async def _scroll_to_load(self, page):
        """Scroll the results panel slowly to trigger lazy loading."""
        try:
            # LinkedIn's layout puts the scrollable list inside a specific container, not the window body
            panel_selector = "div.jobs-search-results-list, div.jobs-search-results__list"
            await page.wait_for_selector(panel_selector, timeout=5000)
            
            for _ in range(8):
                try:
                    await page.evaluate(f"""() => {{
                        const panel = document.querySelector('{panel_selector}');
                        if (panel) panel.scrollBy(0, 1000);
                    }}""")
                    await delay(800, 1500)
                except Exception:
                    break  # Break out if page closes
        except Exception:
            # Fallback to window scroll if the container isn't found
            for _ in range(8):
                try:
                    await page.evaluate("window.scrollBy(0, 800)")
                    await delay(600, 1200)
                except Exception:
                    break
        try:
            await delay(1000, 2000)
        except Exception:
            pass

    async def _is_blocked(self, page) -> bool:
        """Detect CAPTCHA or verification walls."""
        url = page.url
        if "checkpoint" in url or "captcha" in url.lower():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = f"data/screenshots/{timestamp}_blocked.png"
            import os
            os.makedirs("data/screenshots", exist_ok=True)
            await page.screenshot(path=screenshot_path)
            if self.logger:
                self.logger.warn("scraper", f"Blocked by LinkedIn. Screenshot saved: {screenshot_path}")
            return True

        captcha_frame = await page.query_selector("iframe[src*='challenge']")
        if captcha_frame:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = f"data/screenshots/{timestamp}_captcha.png"
            import os
            os.makedirs("data/screenshots", exist_ok=True)
            await page.screenshot(path=screenshot_path)
            if self.logger:
                self.logger.warn("scraper", f"CAPTCHA detected. Screenshot saved: {screenshot_path}")
            return True

        return False

    def _insert_job(self, job: dict):
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            INSERT OR IGNORE INTO jobs
            (id, job_title, company, location, job_url, description, easy_apply, status, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            job["id"], job["job_title"], job["company"], job["location"],
            job["job_url"], job["description"], job["easy_apply"],
            job["status"], job["scraped_at"]
        ))
        conn.commit()
        conn.close()


# LangGraph node function — this is what graph_builder.py registers
@resilient_node("scraper")
async def scraper_node(state: AgentState) -> dict:
    from browser.browser_manager import BrowserManager
    from utils.logger import Logger

    logger = Logger(config.DB_PATH, state["run_id"])
    bm = BrowserManager()

    try:
        await bm.start()
        await bm.login(logger)

        agent = ScraperAgent(bm, logger)
        jobs = await agent.scrape(
            domains=state["confirmed_domains"],
            city=state["confirmed_city"],
            job_type_code=state.get("job_type_code", ""),
            max_per_domain=state.get("max_jobs", 10)
        )

        return {
            "scraped_jobs": jobs,
            "current_phase": "scraped"
        }
    finally:
        await bm.stop()
