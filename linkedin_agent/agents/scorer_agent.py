import json
import sqlite3
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from utils.llm_client import LLMClient
from graph.state import AgentState
import config

console = Console()

SCORE_PROMPT = """You are a job-candidate fit evaluator for an autonomous job application agent.

Given the candidate profile and a job description, return a JSON object evaluating fit.

Scoring criteria (apply in this order of importance):
1. Skills overlap — does the candidate have the required and preferred skills?
2. Experience level — is the role appropriate for the candidate's experience level?
3. Domain relevance — does the job domain match the candidate's target domains?
4. Location — does the job location match the candidate's preferred city?
5. Education — is the candidate's education relevant to the role?

Return a JSON object with exactly these keys:
- score: float between 0.0 and 1.0 (1.0 = perfect match, 0.0 = completely irrelevant)
- reasons: list of exactly 3 short strings explaining the top factors in your score
- missing_skills: list of skills explicitly required or preferred in the job description
  that are NOT present in the candidate profile (empty list if none)

CANDIDATE PROFILE:
{profile_json}

JOB TITLE: {job_title}
COMPANY: {company}
LOCATION: {location}

JOB DESCRIPTION:
{job_description}

Return only valid JSON. No markdown fences. No explanation outside the JSON object.
"""


class ScorerAgent:
    def __init__(self, logger=None):
        self.llm = LLMClient(role="scorer", logger=logger)
        self.logger = logger

    def score_all(self, profile: dict) -> list:
        """
        Fetch all scraped jobs from SQLite, score each one,
        update the DB, return sorted list of scored jobs.
        """
        jobs = self._fetch_scraped_jobs()

        if not jobs:
            if self.logger:
                self.logger.warn("scorer", "No scraped jobs found in DB to score.")
            return []

        if self.logger:
            self.logger.info("scorer", f"Scoring {len(jobs)} jobs against profile...")

        scored = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task("Scoring jobs...", total=len(jobs))

            for job in jobs:
                result = self._score_one(job, profile)
                if result:
                    scored.append(result)
                progress.advance(task)

        # Sort by score descending
        scored.sort(key=lambda j: j["score"], reverse=True)

        # Apply threshold — update status in DB
        above = 0
        below = 0
        for job in scored:
            if job["score"] >= config.SCORE_THRESHOLD:
                self._update_job_status(job["job_url"], "scored", job["score"],
                                        job["reasons"], job["missing_skills"])
                above += 1
            else:
                self._update_job_status(job["job_url"], "skipped", job["score"],
                                        job["reasons"], job["missing_skills"])
                below += 1

        if self.logger:
            self.logger.info("scorer",
                f"Scoring complete. Above threshold: {above} | Skipped: {below}")

        return scored

    def _score_one(self, job: dict, profile: dict) -> dict | None:
        prompt = SCORE_PROMPT.format(
            profile_json=json.dumps(profile, indent=2),
            job_title=job["job_title"],
            company=job["company"],
            location=job["location"],
            job_description=job["description"][:3000]  # cap to avoid token overflow
        )

        try:
            raw = self.llm.complete(prompt)

            # Strip markdown fences if Gemini adds them despite instructions
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()

            parsed = json.loads(clean)

            score = float(parsed.get("score", 0.0))
            reasons = parsed.get("reasons", [])
            missing_skills = parsed.get("missing_skills", [])

            # Clamp score to valid range
            score = max(0.0, min(1.0, score))

            if self.logger:
                self.logger.info(
                    "scorer",
                    f"[{score:.2f}] {job['job_title']} @ {job['company']}"
                )

            return {
                **job,
                "score": score,
                "reasons": reasons,
                "missing_skills": missing_skills
            }

        except json.JSONDecodeError as e:
            if self.logger:
                self.logger.warn(
                    "scorer",
                    f"JSON parse failed for {job['job_title']} @ {job['company']}: {e}"
                )
            # Assign score 0 so it goes to skipped — never crash the pipeline
            return {**job, "score": 0.0, "reasons": ["Parse error"], "missing_skills": []}

        except Exception as e:
            if self.logger:
                self.logger.warn("scorer", f"Scoring failed for {job['job_title']}: {e}")
            return {**job, "score": 0.0, "reasons": [str(e)], "missing_skills": []}

    def _fetch_scraped_jobs(self) -> list:
        conn = sqlite3.connect(config.DB_PATH)
        rows = conn.execute("""
            SELECT id, job_title, company, location, job_url, description
            FROM jobs
            WHERE status = 'scraped'
        """).fetchall()
        conn.close()

        return [
            {
                "id": r[0], "job_title": r[1], "company": r[2],
                "location": r[3], "job_url": r[4], "description": r[5]
            }
            for r in rows
        ]

    def _update_job_status(
        self,
        job_url: str,
        status: str,
        score: float,
        reasons: list,
        missing_skills: list
    ):
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            UPDATE jobs
            SET status = ?, score = ?, reasons = ?, missing_skills = ?
            WHERE job_url = ?
        """, (
            status,
            score,
            json.dumps(reasons),
            json.dumps(missing_skills),
            job_url
        ))
        conn.commit()
        conn.close()


def _print_scored_table(scored_jobs: list, threshold: float):
    """Print a rich summary table of all scored jobs."""
    above = [j for j in scored_jobs if j["score"] >= threshold]
    below = [j for j in scored_jobs if j["score"] < threshold]

    # Above threshold table
    if above:
        table = Table(
            title=f"Jobs Above Threshold (>= {threshold}) — Will Be Applied To",
            show_header=True,
            header_style="bold green"
        )
        table.add_column("Score", style="green", width=7)
        table.add_column("Title", style="cyan", max_width=32)
        table.add_column("Company", style="white", max_width=22)
        table.add_column("Top Reason", style="dim", max_width=40)

        for job in above:
            top_reason = job["reasons"][0] if job["reasons"] else ""
            table.add_row(
                f"{job['score']:.2f}",
                job["job_title"],
                job["company"],
                top_reason
            )
        console.print(table)

    # Below threshold summary
    if below:
        console.print(
            f"\n[dim]Skipped {len(below)} jobs below threshold "
            f"(score < {threshold})[/dim]"
        )

    console.print(
        f"\n[bold]Summary:[/bold] "
        f"[green]{len(above)} to apply[/green] | "
        f"[dim]{len(below)} skipped[/dim]"
    )


# LangGraph node function
def scorer_node(state: AgentState) -> dict:
    from utils.logger import Logger

    logger = Logger(config.DB_PATH, state["run_id"])
    profile = state["user_profile"]

    agent = ScorerAgent(logger=logger)
    scored_jobs = agent.score_all(profile)

    above_threshold = [
        j for j in scored_jobs
        if j["score"] >= config.SCORE_THRESHOLD
    ]
    skipped = [
        j for j in scored_jobs
        if j["score"] < config.SCORE_THRESHOLD
    ]

    _print_scored_table(scored_jobs, config.SCORE_THRESHOLD)

    return {
        "scored_jobs": above_threshold,
        "skipped_count": len(skipped),
        "current_phase": "scored"
    }
