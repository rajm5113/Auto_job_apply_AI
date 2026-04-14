import argparse
import asyncio
import uuid
import os
from rich.console import Console
from graph.graph_builder import build_graph
from graph.state import AgentState
from db.schema import init_db
import config

console = Console()


async def run_pipeline(resume_path: str):
    run_id = str(uuid.uuid4())
    init_db(config.DB_PATH)

    initial_state: AgentState = {
        "run_id": run_id,
        "resume_path": resume_path,
        "raw_resume_text": "",
        "user_profile": {},
        "confirmed_domains": [],
        "confirmed_city": "",
        "job_type_code": "",
        "job_type_label": "Any",
        "max_jobs": 10,
        "scraped_jobs": [],
        "scored_jobs": [],
        "applied_count": 0,
        "manual_review_count": 0,
        "skipped_count": 0,
        "error": None,
        "current_phase": "init"
    }

    console.print(f"\n[bold cyan]LinkedIn Autonomous Job Agent[/bold cyan]")
    console.print(f"[dim]Run ID : {run_id}[/dim]")
    console.print(f"[dim]Resume : {resume_path}[/dim]\n")

    graph = build_graph()
    await graph.ainvoke(initial_state)


def main():
    parser = argparse.ArgumentParser(
        description="LinkedIn Autonomous Job Application Agent"
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Path to your resume PDF"
    )
    args = parser.parse_args()
    
    try:
        asyncio.run(run_pipeline(args.resume))
    except KeyboardInterrupt:
        console.print("\n[yellow bold]Pipeline has been terminated by user.[/yellow bold]")
        os._exit(0)   # Force-kill Playwright browser subprocess


if __name__ == "__main__":
    main()
