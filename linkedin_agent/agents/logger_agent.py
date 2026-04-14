import json
import sqlite3
from datetime import datetime
from rich.console import Console
from rich.table import Table
from utils.sheets_client import SheetsClient
from graph.state import AgentState
import config

console = Console()


class LoggerAgent:
    def __init__(self, sheets_client: SheetsClient, logger=None):
        self.sheets = sheets_client
        self.logger = logger

    def log_all_pending(self) -> dict:
        """
        Fetch all jobs with status 'applied' or 'manual_review' that have
        not yet been logged (logged = 0). Write them to Google Sheets in
        two batch calls — one per tab. Mark them logged = 1 in SQLite.
        Returns counts for the run summary.
        """
        applied_jobs = self._fetch_unlogged("applied")
        manual_jobs  = self._fetch_unlogged("manual_review")

        if self.logger:
            self.logger.info(
                "logger",
                f"Logging {len(applied_jobs)} applied + "
                f"{len(manual_jobs)} manual review jobs to Sheets"
            )

        # Build row arrays for batch write
        applied_rows = [
            [
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                job["job_title"],
                job["company"],
                job["location"],
                job["job_url"],
                f"{job['score']:.2f}" if job["score"] else "N/A",
                "applied"
            ]
            for job in applied_jobs
        ]

        manual_rows = [
            [
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                job["job_title"],
                job["company"],
                job["location"],
                job["job_url"],
                job["fail_reason"] or "unknown",
                "needs_review"
            ]
            for job in manual_jobs
        ]

        # Write to Sheets
        sheets_ok = True
        try:
            if applied_rows:
                self.sheets.batch_append(
                    config.SHEETS_ID, "Applied Jobs", applied_rows
                )
                if self.logger:
                    self.logger.info(
                        "logger",
                        f"Wrote {len(applied_rows)} rows to 'Applied Jobs' tab"
                    )

            if manual_rows:
                self.sheets.batch_append(
                    config.SHEETS_ID, "Manual Review", manual_rows
                )
                if self.logger:
                    self.logger.info(
                        "logger",
                        f"Wrote {len(manual_rows)} rows to 'Manual Review' tab"
                    )

        except Exception as e:
            sheets_ok = False
            if self.logger:
                self.logger.error(
                    "logger",
                    f"Sheets write failed: {e}. Jobs are still saved in SQLite."
                )

        # Mark all as logged in SQLite regardless of Sheets outcome
        # SQLite is always the source of truth
        all_urls = (
            [j["job_url"] for j in applied_jobs] +
            [j["job_url"] for j in manual_jobs]
        )
        if all_urls:
            self._mark_logged(all_urls)

        return {
            "applied_logged": len(applied_jobs),
            "manual_logged": len(manual_jobs),
            "sheets_ok": sheets_ok
        }

    def _fetch_unlogged(self, status: str) -> list:
        conn = sqlite3.connect(config.DB_PATH)
        rows = conn.execute("""
            SELECT id, job_title, company, location, job_url,
                   score, fail_reason
            FROM jobs
            WHERE status = ? AND logged = 0
        """, (status,)).fetchall()
        conn.close()
        return [
            {
                "id": r[0], "job_title": r[1], "company": r[2],
                "location": r[3], "job_url": r[4],
                "score": r[5], "fail_reason": r[6]
            }
            for r in rows
        ]

    def _mark_logged(self, job_urls: list):
        conn = sqlite3.connect(config.DB_PATH)
        placeholders = ",".join("?" * len(job_urls))
        conn.execute(
            f"UPDATE jobs SET logged = 1 WHERE job_url IN ({placeholders})",
            job_urls
        )
        conn.commit()
        conn.close()


def _print_final_summary(state: AgentState, log_result: dict):
    """Print the complete run summary table to CLI."""
    scraped       = len(state.get("scraped_jobs", []))
    scored        = len(state.get("scored_jobs", []))
    skipped       = state.get("skipped_count", 0)
    applied       = state.get("applied_count", 0)
    manual_review = state.get("manual_review_count", 0)
    sheets_status = "yes" if log_result.get("sheets_ok") else "FAILED (check logs)"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="dim", width=22)
    table.add_column("Value", style="bold white")

    table.add_row("Scraped", str(scraped))
    table.add_row("Scored", str(scored + skipped))
    table.add_row("Above threshold", str(scored))
    table.add_row("Applied", f"[green]{applied}[/green]")
    table.add_row("Manual review", f"[yellow]{manual_review}[/yellow]")
    table.add_row("Skipped", f"[dim]{skipped}[/dim]")
    table.add_row("Logged to Sheets", sheets_status)

    console.print("\n" + "=" * 38)
    console.print("[bold]  Run Complete[/bold]")
    console.print("=" * 38)
    console.print(table)
    console.print("=" * 38 + "\n")


# LangGraph node function
def logger_node(state: AgentState) -> dict:
    from utils.logger import Logger

    logger = Logger(config.DB_PATH, state["run_id"])

    try:
        sheets = SheetsClient().authenticate()
        agent = LoggerAgent(sheets, logger=logger)
        result = agent.log_all_pending()
        _print_final_summary(state, result)

        return {
            "current_phase": "logged"
        }

    except FileNotFoundError:
        logger.info("logger", "Google Sheets disabled: service_account.json not found. Application records saved locally to jobs.db.")
        _print_final_summary(state, {"sheets_ok": False})
        return {"current_phase": "logged"}

    except Exception as e:
        logger.error("logger", f"Logger node failed: {e}")
        # Do not set error state — logging failure must never crash the pipeline
        # The user still has all data in SQLite
        _print_final_summary(state, {"sheets_ok": False})
        return {"current_phase": "logged_with_error"}
