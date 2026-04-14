from dotenv import load_dotenv
import os

load_dotenv()

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise ValueError(f"Missing required env var: {key}")
    return val

GEMINI_API_KEY: str    = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY: str      = _require("GROQ_API_KEY")
LINKEDIN_EMAIL: str    = _require("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD: str = _require("LINKEDIN_PASSWORD")
SCORE_THRESHOLD: float = float(_require("SCORE_THRESHOLD"))
DB_PATH: str           = _require("DB_PATH")
PROFILE_PATH: str      = _require("PROFILE_PATH")
SHEETS_ID: str         = _require("SHEETS_ID")
RESUME_PATH: str       = _require("RESUME_PATH")
USE_GEMINI: bool       = os.getenv("USE_GEMINI", "false").lower() == "true"

