import json
import os
import asyncio
from patchright.async_api import async_playwright
from browser.human_delay import delay
import config

COOKIE_PATH = "data/linkedin_session.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT
        )
        self.page = await self.context.new_page()

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def login(self, logger=None):
        """Log in or restore session. Always call this after start()."""
        if os.path.exists(COOKIE_PATH):
            if logger:
                logger.info("browser", "Cookie file found — attempting session restore")
            await self._restore_session(logger)
        else:
            if logger:
                logger.info("browser", "No cookie file — performing fresh login")
            await self._fresh_login(logger)

    async def _restore_session(self, logger=None):
        with open(COOKIE_PATH, "r") as f:
            cookies = json.load(f)
        await self.context.add_cookies(cookies)
        await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await delay(1500, 3000)

        if "feed" not in self.page.url:
            if logger:
                logger.warn("browser", "Session expired — performing fresh login")
            os.remove(COOKIE_PATH)
            await self._fresh_login(logger)
        else:
            if logger:
                logger.info("browser", "Session restored successfully")

    async def _fresh_login(self, logger=None):
        await self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        await delay(1000, 2000)

        await self.page.fill('input[name="session_key"]', config.LINKEDIN_EMAIL)
        await delay(400, 800)
        await self.page.fill('input[name="session_password"]', config.LINKEDIN_PASSWORD)
        await delay(600, 1200)
        await self.page.click('button[type="submit"]')
        await delay(3000, 5000)

        if "feed" not in self.page.url and "checkpoint" not in self.page.url:
            raise RuntimeError(
                f"Login failed — ended up at: {self.page.url}. "
                "Check your LINKEDIN_EMAIL and LINKEDIN_PASSWORD in .env"
            )

        if "checkpoint" in self.page.url:
            if logger:
                logger.warn("browser", "LinkedIn is asking for verification. Complete it manually in the browser window, then press Enter here.")
            input("Press Enter after completing LinkedIn verification...")
            await delay(2000, 3000)

        cookies = await self.context.cookies()
        os.makedirs("data", exist_ok=True)
        with open(COOKIE_PATH, "w") as f:
            json.dump(cookies, f, indent=2)

        if logger:
            logger.info("browser", f"Login successful. Cookies saved to {COOKIE_PATH}")
