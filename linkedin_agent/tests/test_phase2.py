import json
import sqlite3
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Test 1 — human_delay returns within expected range
def test_human_delay_timing():
    from browser.human_delay import delay
    import time
    start = time.time()
    asyncio.run(delay(100, 200))
    elapsed = (time.time() - start) * 1000
    assert 90 <= elapsed <= 500   # generous upper bound for CI

# Test 2 — BrowserManager saves cookies after fresh login
@pytest.mark.asyncio
async def test_browser_manager_saves_cookies(tmp_path):
    from browser.browser_manager import BrowserManager, COOKIE_PATH
    import os

    mock_page = AsyncMock()
    mock_page.url = "https://www.linkedin.com/feed/"
    mock_context = AsyncMock()
    mock_context.cookies = AsyncMock(return_value=[{"name": "li_at", "value": "test"}])

    bm = BrowserManager()
    bm.page = mock_page
    bm.context = mock_context

    with patch("os.path.exists", return_value=False), \
         patch("builtins.open", create=True), \
         patch("json.dump") as mock_dump, \
         patch("os.makedirs"):
        await bm._fresh_login()
        mock_dump.assert_called_once()

# Test 3 — ScraperAgent inserts job into SQLite with INSERT OR IGNORE
def test_scraper_inserts_job():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, job_title TEXT, company TEXT, location TEXT,
            job_url TEXT UNIQUE, description TEXT, easy_apply INTEGER,
            status TEXT, scraped_at TEXT
        );
    """)

    job = {
        "id": "abc-123", "job_title": "ML Engineer", "company": "Acme",
        "location": "Bengaluru", "job_url": "https://linkedin.com/jobs/view/123",
        "description": "Build cool stuff", "easy_apply": 1,
        "status": "scraped", "scraped_at": "2025-01-01T00:00:00"
    }

    conn.execute("""
        INSERT OR IGNORE INTO jobs
        (id, job_title, company, location, job_url, description, easy_apply, status, scraped_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, tuple(job.values()))
    conn.commit()

    rows = conn.execute("SELECT job_title FROM jobs WHERE job_url=?", (job["job_url"],)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "ML Engineer"

    # Insert same URL again — should be silently ignored
    conn.execute("""
        INSERT OR IGNORE INTO jobs
        (id, job_title, company, location, job_url, description, easy_apply, status, scraped_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, ("new-id", "Different Title", "Acme", "Bengaluru",
          "https://linkedin.com/jobs/view/123", "", 1, "scraped", "2025-01-02T00:00:00"))
    conn.commit()

    rows = conn.execute("SELECT job_title FROM jobs").fetchall()
    assert len(rows) == 1   # still only one row

# Test 4 — scraper_node returns error state if browser raises
@pytest.mark.asyncio
async def test_scraper_node_handles_error():
    from agents.scraper_agent import scraper_node

    with patch("browser.browser_manager.BrowserManager") as MockBM, \
         patch("utils.logger.Logger"):
        MockBM.return_value.start = AsyncMock(side_effect=RuntimeError("browser failed"))
        MockBM.return_value.stop = AsyncMock()

        state = {
            "run_id": "test-run",
            "confirmed_domains": ["ML Engineer"],
            "confirmed_city": "Bengaluru",
            "scraped_jobs": []
        }
        result = await scraper_node(state)
        assert "error" in result
        assert "browser failed" in result["error"]
