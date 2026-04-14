import json
import sqlite3
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime


# Helper — seed DB with one applied and one manual_review job
def _seed_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT, job_title TEXT, company TEXT, location TEXT,
            job_url TEXT UNIQUE, description TEXT, score REAL,
            status TEXT DEFAULT 'scraped', logged INTEGER DEFAULT 0,
            scraped_at TEXT, applied_at TEXT, fail_reason TEXT,
            reasons TEXT, missing_skills TEXT
        );
        INSERT INTO jobs
            (id, job_title, company, location, job_url, score, status, logged)
        VALUES
            ('1','ML Engineer','Acme','Bengaluru',
             'https://linkedin.com/jobs/1', 0.85, 'applied', 0),
            ('2','Data Analyst','Beta','Bengaluru',
             'https://linkedin.com/jobs/2', 0.70, 'manual_review', 0),
            ('3','Already Logged','Gamma','Bengaluru',
             'https://linkedin.com/jobs/3', 0.90, 'applied', 1);
    """)
    conn.commit()


# Test 1 — fetch_unlogged returns only unlogged jobs of correct status
def test_fetch_unlogged_filters_correctly():
    real_conn = sqlite3.connect(":memory:")
    _seed_db(real_conn)

    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.close = MagicMock()

    with patch("agents.logger_agent.sqlite3.connect", return_value=mock_conn), \
         patch("agents.logger_agent.config.DB_PATH", ":memory:"):
        from agents.logger_agent import LoggerAgent
        agent = LoggerAgent(MagicMock())

        applied  = agent._fetch_unlogged("applied")
        manual   = agent._fetch_unlogged("manual_review")
        already  = [j for j in applied if j["job_url"] == "https://linkedin.com/jobs/3"]

    real_conn.close()

    assert len(applied) == 1
    assert applied[0]["job_title"] == "ML Engineer"
    assert len(manual) == 1
    assert manual[0]["job_title"] == "Data Analyst"
    assert len(already) == 0   # already logged job must not appear


# Test 2 — mark_logged sets logged = 1 for given URLs
def test_mark_logged_updates_db():
    real_conn = sqlite3.connect(":memory:")
    _seed_db(real_conn)

    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.close = MagicMock()

    with patch("agents.logger_agent.sqlite3.connect", return_value=mock_conn), \
         patch("agents.logger_agent.config.DB_PATH", ":memory:"):
        from agents.logger_agent import LoggerAgent
        agent = LoggerAgent(MagicMock())
        agent._mark_logged(["https://linkedin.com/jobs/1"])

    row = real_conn.execute(
        "SELECT logged FROM jobs WHERE job_url=?",
        ("https://linkedin.com/jobs/1",)
    ).fetchone()
    real_conn.close()
    assert row[0] == 1


# Test 3 — log_all_pending calls batch_append with correct tab names
def test_log_all_pending_writes_correct_tabs():
    real_conn = sqlite3.connect(":memory:")
    _seed_db(real_conn)

    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.close = MagicMock()

    mock_sheets = MagicMock()

    with patch("agents.logger_agent.sqlite3.connect", return_value=mock_conn), \
         patch("agents.logger_agent.config.DB_PATH", ":memory:"), \
         patch("agents.logger_agent.config.SHEETS_ID", "fake-sheet-id"):
        from agents.logger_agent import LoggerAgent
        agent = LoggerAgent(mock_sheets)
        result = agent.log_all_pending()

    real_conn.close()

    # batch_append should be called twice — once per tab
    assert mock_sheets.batch_append.call_count == 2

    call_args = [c[0] for c in mock_sheets.batch_append.call_args_list]
    tab_names = [args[1] for args in call_args]
    assert "Applied Jobs" in tab_names
    assert "Manual Review" in tab_names

    assert result["applied_logged"] == 1
    assert result["manual_logged"] == 1
    assert result["sheets_ok"] is True


# Test 4 — Sheets failure does not crash logger, marks jobs logged in SQLite anyway
def test_sheets_failure_does_not_crash():
    real_conn = sqlite3.connect(":memory:")
    _seed_db(real_conn)

    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.close = MagicMock()

    mock_sheets = MagicMock()
    mock_sheets.batch_append.side_effect = Exception("Sheets API quota exceeded")

    with patch("agents.logger_agent.sqlite3.connect", return_value=mock_conn), \
         patch("agents.logger_agent.config.DB_PATH", ":memory:"), \
         patch("agents.logger_agent.config.SHEETS_ID", "fake-sheet-id"):
        from agents.logger_agent import LoggerAgent
        agent = LoggerAgent(mock_sheets)
        result = agent.log_all_pending()   # must not raise

    assert result["sheets_ok"] is False
    # Jobs must still be marked logged in SQLite
    row = real_conn.execute(
        "SELECT logged FROM jobs WHERE job_url=?",
        ("https://linkedin.com/jobs/1",)
    ).fetchone()
    real_conn.close()
    assert row[0] == 1


# Test 5 — SheetsClient raises FileNotFoundError if credentials missing
def test_sheets_client_raises_without_credentials():
    from utils.sheets_client import SheetsClient
    with patch("os.path.exists", return_value=False):
        client = SheetsClient()
        with pytest.raises(FileNotFoundError):
            client.authenticate()


# Test 6 — logger_node does not set error state on Sheets failure
def test_logger_node_never_sets_error_on_sheets_failure():
    with patch("agents.logger_agent.SheetsClient") as MockSheets, \
         patch("utils.logger.Logger"), \
         patch("agents.logger_agent._print_final_summary"):
        MockSheets.return_value.authenticate.return_value = MockSheets.return_value
        MockSheets.return_value.batch_append.side_effect = Exception("quota")

        mock_agent = MagicMock()
        mock_agent.log_all_pending.return_value = {
            "applied_logged": 0, "manual_logged": 0, "sheets_ok": False
        }

        with patch("agents.logger_agent.LoggerAgent", return_value=mock_agent):
            from agents.logger_agent import logger_node
            state = {
                "run_id": "test-run",
                "scraped_jobs": [], "scored_jobs": [],
                "applied_count": 0, "manual_review_count": 0,
                "skipped_count": 0
            }
            result = logger_node(state)

    assert result.get("error") is None
    assert "logged" in result.get("current_phase", "")
