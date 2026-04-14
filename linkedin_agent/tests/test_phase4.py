import json
import sqlite3
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# Helper
def _make_job(score=0.80):
    return {
        "id": "job-1", "job_title": "ML Engineer", "company": "Acme",
        "location": "Bengaluru", "job_url": "https://linkedin.com/jobs/1",
        "description": "Build ML pipelines", "score": score
    }

def _make_profile():
    return {
        "name": "Raj", "email": "raj@example.com", "phone": "9999999999",
        "city": "Bengaluru", "skills": ["Python", "ML", "LangChain"],
        "experience_years": 1
    }


# Test 1 — _mark_applied sets status to 'applied' in SQLite
def test_mark_applied():
    real_conn = sqlite3.connect(":memory:")
    real_conn.executescript("""
        CREATE TABLE jobs (
            id TEXT, job_title TEXT, company TEXT, location TEXT,
            job_url TEXT UNIQUE, description TEXT, score REAL,
            status TEXT DEFAULT 'scored', applied_at TEXT, fail_reason TEXT,
            logged INTEGER DEFAULT 0
        );
        INSERT INTO jobs (id, job_title, company, location, job_url,
                          description, score, status)
        VALUES ('1','ML Eng','Acme','Bengaluru',
                'https://linkedin.com/jobs/1','desc',0.85,'scored');
    """)

    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.close = MagicMock()   # no-op close

    with patch("agents.applier_agent.sqlite3.connect", return_value=mock_conn), \
         patch("agents.applier_agent.config.DB_PATH", ":memory:"):
        from agents.applier_agent import ApplierAgent
        agent = ApplierAgent(MagicMock())
        agent._mark_applied("https://linkedin.com/jobs/1")

    row = real_conn.execute(
        "SELECT status FROM jobs WHERE job_url=?",
        ("https://linkedin.com/jobs/1",)
    ).fetchone()
    real_conn.close()
    assert row[0] == "applied"


# Test 2 — _mark_manual_review sets correct status and reason
def test_mark_manual_review():
    real_conn = sqlite3.connect(":memory:")
    real_conn.executescript("""
        CREATE TABLE jobs (
            id TEXT, job_url TEXT UNIQUE, status TEXT,
            fail_reason TEXT, applied_at TEXT, logged INTEGER DEFAULT 0
        );
        INSERT INTO jobs (id, job_url, status)
        VALUES ('1','https://linkedin.com/jobs/1','scored');
    """)

    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.close = MagicMock()

    with patch("agents.applier_agent.sqlite3.connect", return_value=mock_conn), \
         patch("agents.applier_agent.config.DB_PATH", ":memory:"):
        from agents.applier_agent import ApplierAgent
        agent = ApplierAgent(MagicMock())
        agent._mark_manual_review(
            "https://linkedin.com/jobs/1", "easy_apply_button_not_found"
        )

    row = real_conn.execute(
        "SELECT status, fail_reason FROM jobs WHERE job_url=?",
        ("https://linkedin.com/jobs/1",)
    ).fetchone()
    real_conn.close()
    assert row[0] == "manual_review"
    assert row[1] == "easy_apply_button_not_found"


# Test 3 — cover letter generation returns non-empty string
def test_cover_letter_generation():
    from agents.applier_agent import ApplierAgent

    agent = ApplierAgent(MagicMock())
    with patch.object(agent.llm, "complete", return_value="Great cover letter."):
        result = asyncio.run(
            agent._generate_cover_letter(_make_job(), _make_profile())
        )
    assert len(result) > 0
    assert isinstance(result, str)


# Test 4 — applier_node returns error state if browser fails to start
@pytest.mark.asyncio
async def test_applier_node_handles_browser_error():
    from agents.applier_agent import applier_node

    with patch("browser.browser_manager.BrowserManager") as MockBM, \
         patch("utils.logger.Logger"):
        MockBM.return_value.start = AsyncMock(
            side_effect=RuntimeError("chromium not found")
        )
        MockBM.return_value.stop = AsyncMock()

        state = {
            "run_id": "test-run",
            "user_profile": _make_profile(),
            "scored_jobs": [_make_job()]
        }
        result = await applier_node(state)

    assert "error" in result
    assert "chromium" in result["error"]


# Test 5 — _match_text_field maps known labels correctly
def test_match_text_field():
    from agents.applier_agent import ApplierAgent
    agent = ApplierAgent(MagicMock())
    profile = _make_profile()

    assert agent._match_text_field("Current city", profile) == "Bengaluru"
    assert agent._match_text_field("Expected salary", profile) == "As per industry standard"
    assert agent._match_text_field("Notice period", profile) == "Immediately"
    assert agent._match_text_field("Some unknown field xyz", profile) is None
