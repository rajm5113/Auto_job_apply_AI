from typing import TypedDict, Optional

class AgentState(TypedDict):
    # Run metadata
    run_id: str
    resume_path: str

    # Resume and profile
    raw_resume_text: str
    user_profile: dict

    # User preferences confirmed in CLI
    confirmed_domains: list
    confirmed_city: str
    job_type_code: str    # LinkedIn f_JT param: "F", "I", "P", "C", or ""
    job_type_label: str   # Human readable: "Full-Time", "Internship" etc.
    max_jobs: int         # Max jobs to scrape/apply to limit API usage

    # Scraping (Phase 2)
    scraped_jobs: list

    # Scoring (Phase 3)
    scored_jobs: list

    # Application results (Phase 4)
    applied_count: int
    manual_review_count: int
    skipped_count: int

    # Control flow
    error: Optional[str]
    current_phase: str
