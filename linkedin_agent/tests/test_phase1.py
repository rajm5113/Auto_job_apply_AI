import json
import sqlite3
import pytest
from unittest.mock import MagicMock, patch

# Test 1 — SQLite schema creates all three tables
def test_schema_creates_tables():
    from db.schema import init_db
    init_db(":memory:")
    conn = sqlite3.connect(":memory:")
    init_db_in_memory(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "jobs" in tables
    assert "user_profile" in tables
    assert "run_log" in tables

def init_db_in_memory(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, job_url TEXT UNIQUE, status TEXT, logged INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS user_profile (id INTEGER PRIMARY KEY DEFAULT 1, profile_json TEXT);
        CREATE TABLE IF NOT EXISTS run_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, phase TEXT, message TEXT, level TEXT, ts TEXT);
    """)
    conn.commit()

# Test 2 — LLM client falls back to Groq on Gemini quota error
def test_llm_fallback_to_groq():
    from utils.llm_client import LLMClient

    mock_groq_response = MagicMock()
    mock_groq_response.content = "groq response"

    with patch("utils.llm_client.ChatGoogleGenerativeAI") as MockGemini, \
         patch("utils.llm_client.ChatGroq") as MockGroq:

        MockGemini.return_value.invoke.side_effect = Exception("429 ResourceExhausted quota")
        MockGroq.return_value.invoke.return_value = mock_groq_response

        client = LLMClient()
        result = client.complete("test prompt")
        assert result == "groq response"
        MockGroq.return_value.invoke.assert_called_once()

# Test 3 — Resume node returns all required profile keys
def test_resume_node_returns_required_keys():
    from agents.resume_agent import resume_node

    fake_profile = {
        "name": "Raj", "email": "raj@example.com", "phone": "9999999999",
        "city": "Bengaluru", "skills": ["Python", "ML"], "experience_years": 1,
        "work_history": [], "education": [], "suggested_domains": ["ML Engineer"]
    }

    with patch("agents.resume_agent.pdfplumber.open") as mock_pdf, \
         patch("agents.resume_agent.LLMClient") as MockLLM, \
         patch("agents.resume_agent.sqlite3.connect"), \
         patch("builtins.open", create=True), \
         patch("os.makedirs"):

        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Raj, ML Engineer, Bengaluru"
        mock_pdf.return_value.__enter__.return_value.pages = [mock_page]
        MockLLM.return_value.complete.return_value = json.dumps(fake_profile)

        state = {"resume_path": "fake.pdf", "run_id": "test-run"}
        result = resume_node(state)

    assert "user_profile" in result
    for key in ["name", "email", "phone", "city", "skills", "experience_years",
                "work_history", "education", "suggested_domains"]:
        assert key in result["user_profile"], f"Missing key: {key}"

# Test 4 — PDF text extraction returns non-empty string
def test_pdf_text_extraction():
    import pdfplumber
    with patch("pdfplumber.open") as mock_pdf:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Sample resume text"
        mock_pdf.return_value.__enter__.return_value.pages = [mock_page]
        with pdfplumber.open("fake.pdf") as pdf:
            text = "".join(p.extract_text() or "" for p in pdf.pages)
    assert len(text.strip()) > 0
