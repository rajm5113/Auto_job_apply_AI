import json
import sqlite3
import pytest
from unittest.mock import MagicMock, patch

# Helper — build an in-memory DB with one scraped job
def _seed_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, job_title TEXT, company TEXT,
            location TEXT, job_url TEXT UNIQUE, description TEXT,
            easy_apply INTEGER DEFAULT 1, score REAL,
            reasons TEXT, missing_skills TEXT,
            status TEXT DEFAULT 'scraped', logged INTEGER DEFAULT 0,
            scraped_at TEXT, applied_at TEXT, fail_reason TEXT
        );
    """)
    conn.execute("""
        INSERT INTO jobs (id, job_title, company, location, job_url, description, status)
        VALUES ('abc', 'ML Engineer', 'Acme', 'Bengaluru',
                'https://linkedin.com/jobs/1', 'Build ML models with Python', 'scraped')
    """)
    conn.commit()


# Test 1 — score is clamped between 0.0 and 1.0 even if LLM returns out-of-range
def test_score_clamping():
    from agents.scorer_agent import ScorerAgent

    agent = ScorerAgent()

    with patch.object(agent.llm, "complete", return_value=json.dumps({
        "score": 1.5,   # out of range high
        "reasons": ["a", "b", "c"],
        "missing_skills": []
    })):
        fake_job = {
            "id": "1", "job_title": "ML Eng", "company": "X",
            "location": "Bengaluru", "job_url": "http://x.com", "description": "..."
        }
        result = agent._score_one(fake_job, {"skills": ["Python"]})
        assert result["score"] == 1.0


# Test 2 — jobs below threshold get status 'skipped' in DB
def test_below_threshold_marked_skipped():
    from unittest.mock import MagicMock

    real_conn = sqlite3.connect(":memory:")
    _seed_db(real_conn)

    # Wrap real connection in a MagicMock so .close() is a no-op
    # but all other calls (execute, commit) still proxy to the real connection.
    mock_conn = MagicMock(wraps=real_conn)
    mock_conn.close = MagicMock()   # override close → no-op

    with patch("agents.scorer_agent.sqlite3.connect", return_value=mock_conn), \
         patch("agents.scorer_agent.config.SCORE_THRESHOLD", 0.65):

        from agents.scorer_agent import ScorerAgent
        agent = ScorerAgent()
        agent._update_job_status(
            "https://linkedin.com/jobs/1", "skipped",
            0.40, ["low skill match"], ["TensorFlow"]
        )

    row = real_conn.execute(
        "SELECT status, score FROM jobs WHERE job_url=?",
        ("https://linkedin.com/jobs/1",)
    ).fetchone()
    real_conn.close()

    assert row[0] == "skipped"
    assert row[1] == pytest.approx(0.40, abs=0.01)



# Test 3 — LLM JSON parse failure does not crash scorer, assigns score 0
def test_scorer_handles_bad_llm_json():
    from agents.scorer_agent import ScorerAgent

    agent = ScorerAgent()

    with patch.object(agent.llm, "complete", return_value="this is not json at all"):
        fake_job = {
            "id": "1", "job_title": "Data Sci", "company": "Y",
            "location": "Bengaluru", "job_url": "http://y.com", "description": "stuff"
        }
        result = agent._score_one(fake_job, {"skills": ["Python"]})
        assert result is not None
        assert result["score"] == 0.0


# Test 4 — scorer_node returns correct state shape
def test_scorer_node_state_shape():
    from agents.scorer_agent import scorer_node

    fake_scored = [
        {"job_title": "ML Eng", "company": "A", "job_url": "http://a.com",
         "score": 0.85, "reasons": ["x","y","z"], "missing_skills": [],
         "id": "1", "location": "Bengaluru", "description": "..."},
        {"job_title": "DS Intern", "company": "B", "job_url": "http://b.com",
         "score": 0.45, "reasons": ["x","y","z"], "missing_skills": ["Spark"],
         "id": "2", "location": "Bengaluru", "description": "..."},
    ]

    with patch("agents.scorer_agent.ScorerAgent") as MockAgent, \
         patch("utils.logger.Logger"), \
         patch("agents.scorer_agent._print_scored_table"), \
         patch("agents.scorer_agent.config.SCORE_THRESHOLD", 0.65):

        MockAgent.return_value.score_all.return_value = fake_scored

        state = {
            "run_id": "test-run",
            "user_profile": {"skills": ["Python"]},
            "scraped_jobs": fake_scored
        }
        result = scorer_node(state)

    assert "scored_jobs" in result
    assert "skipped_count" in result
    # Only job with score 0.85 should be in scored_jobs
    assert len(result["scored_jobs"]) == 1
    assert result["scored_jobs"][0]["score"] == 0.85
    assert result["skipped_count"] == 1
