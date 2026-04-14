import json
import os
import sqlite3
from datetime import datetime
import pdfplumber
from rich.console import Console
from rich.table import Table
from graph.state import AgentState
from utils.llm_client import LLMClient
from utils.decorators import resilient_node
import config

console = Console()

@resilient_node("resume_parser")
def resume_node(state: AgentState) -> dict:
    llm = LLMClient(role="parser")
    resume_path = state["resume_path"]

    # --- Resume cache check ---
    # If profile JSON already exists for this exact resume file, skip re-parsing.
    cache_meta_path = config.PROFILE_PATH + ".meta"
    cached_resume_path = None
    if os.path.exists(cache_meta_path):
        with open(cache_meta_path, "r") as f:
            cached_resume_path = f.read().strip()

    if cached_resume_path == resume_path and os.path.exists(config.PROFILE_PATH):
        with open(config.PROFILE_PATH, "r") as f:
            profile_dict = json.load(f)
        console.print("[dim]Resume already parsed — using cached profile. Delete profiles/user_profile.json to re-parse.[/dim]")

        table = Table(title="Cached Resume Profile", show_header=True)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")
        table.add_row("Name",       profile_dict.get("name", ""))
        table.add_row("Email",      profile_dict.get("email", ""))
        table.add_row("Phone",      profile_dict.get("phone", ""))
        table.add_row("City",       profile_dict.get("city", ""))
        table.add_row("Experience", f"{profile_dict.get('experience_years', 0)} years")
        table.add_row("Skills",     ", ".join(profile_dict.get("skills", [])))
        console.print(table)

        return {
            "raw_resume_text": "",
            "user_profile": profile_dict,
            "current_phase": "resume_parsed"
        }

    # --- Fresh parse ---
    raw_text = ""
    with pdfplumber.open(resume_path) as pdf:
        for page in pdf.pages:
            raw_text += (page.extract_text() or "") + "\n"

    if not raw_text.strip():
        raise ValueError("Could not extract text from PDF. Is it a scanned image?")

    prompt = f"""You are a resume parser. Extract structured information from the resume text below.
Return ONLY a valid JSON object with exactly these keys:
- name (string)
- email (string)
- phone (string)
- city (string — the candidate's current city only)
- skills (list of strings)
- experience_years (integer)
- work_history (list of objects with keys: title, company, duration)
- education (list of objects with keys: degree, institution)
- suggested_domains (list of 5 to 8 job domain strings suitable for this candidate,
  e.g. "Machine Learning Engineer", "Data Scientist", "AI Research Intern")

Resume text:
{raw_text}

Return only the JSON. No markdown code fences. No explanation outside the JSON."""

    raw_response = llm.complete(prompt)

    clean = raw_response.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    profile_dict = json.loads(clean)

    # Upsert into SQLite
    now = datetime.now().isoformat()
    conn = sqlite3.connect(config.DB_PATH)
    existing = conn.execute("SELECT id FROM user_profile WHERE id = 1").fetchone()
    if existing:
        conn.execute(
            "UPDATE user_profile SET raw_text=?, profile_json=?, updated_at=? WHERE id=1",
            (raw_text, json.dumps(profile_dict), now)
        )
    else:
        conn.execute(
            "INSERT INTO user_profile (id, raw_text, profile_json, created_at, updated_at) VALUES (1,?,?,?,?)",
            (raw_text, json.dumps(profile_dict), now, now)
        )
    conn.commit()
    conn.close()

    # Save profile JSON + cache metadata
    os.makedirs(os.path.dirname(config.PROFILE_PATH), exist_ok=True)
    with open(config.PROFILE_PATH, "w") as f:
        json.dump(profile_dict, f, indent=2)
    with open(cache_meta_path, "w") as f:
        f.write(resume_path)

    # Print to CLI
    table = Table(title="Parsed Resume Profile", show_header=True)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Name",       profile_dict.get("name", ""))
    table.add_row("Email",      profile_dict.get("email", ""))
    table.add_row("Phone",      profile_dict.get("phone", ""))
    table.add_row("City",       profile_dict.get("city", ""))
    table.add_row("Experience", f"{profile_dict.get('experience_years', 0)} years")
    table.add_row("Skills",     ", ".join(profile_dict.get("skills", [])))
    console.print(table)

    return {
        "raw_resume_text": raw_text,
        "user_profile": profile_dict,
        "current_phase": "resume_parsed"
    }


# LinkedIn f_JT param values
_JOB_TYPE_MAP = {
    "1": ("Full-Time",  "F"),
    "2": ("Part-Time",  "P"),
    "3": ("Internship", "I"),
    "4": ("Contract",   "C"),
    "5": ("Any",        ""),
}


@resilient_node("domain_confirm")
def domain_confirm_node(state: AgentState) -> dict:
    profile = state["user_profile"]
    domains = profile.get("suggested_domains", [])
    detected_city = profile.get("city", "")
    exp_years = profile.get("experience_years", 0)

    console.print("\n[bold cyan]Suggested job domains from your resume:[/bold cyan]")
    for i, d in enumerate(domains, 1):
        console.print(f"  {i}. {d}")

    # SELECT mode: user picks which ones they want or enters a custom search string
    console.print("\n[yellow]Type the exact job title or search phrase you want to use (e.g. [bold]Data Analyst Internship[/bold]).[/yellow]")
    console.print("[yellow]Or type the numbers of the suggested domains above (e.g. [bold]1[/bold] or [bold]1,3[/bold]). Press Enter with no input to select ALL:[/yellow]")
    user_input = input("> ").strip()

    if user_input:
        # Check if the user entered numbers or a raw string
        if all(part.strip().isdigit() for part in user_input.split(",")):
            try:
                selected_indices = {int(x.strip()) - 1 for x in user_input.split(",")}
                domains = [d for i, d in enumerate(domains) if i in selected_indices]
                if not domains:
                    console.print("[red]No valid selections — keeping all domains.[/red]")
                    domains = profile.get("suggested_domains", [])
            except ValueError:
                console.print("[red]Invalid input — keeping all domains.[/red]")
        else:
            # The user typed a raw custom string, use it directly!
            domains = [user_input]

    console.print(f"\n[yellow]City for job search (detected: [bold]{detected_city}[/bold]). Press Enter to confirm or type a new city:[/yellow]")
    city_input = input("> ").strip()
    final_city = city_input if city_input else detected_city

    # --- Job type selection ---
    auto_suggest = "3" if exp_years == 0 else "1"
    auto_label   = _JOB_TYPE_MAP[auto_suggest][0]

    console.print(f"\n[bold cyan]What type of jobs are you targeting?[/bold cyan]")
    console.print(f"  1. Full-Time")
    console.print(f"  2. Part-Time")
    console.print(f"  3. Internship")
    console.print(f"  4. Contract")
    console.print(f"  5. Any (no filter)")
    console.print(f"\n[yellow]Press Enter to accept suggestion ([bold]{auto_suggest}. {auto_label}[/bold]) or type a number:[/yellow]")
    jt_input = input("> ").strip()

    jt_key   = jt_input if jt_input in _JOB_TYPE_MAP else auto_suggest
    jt_label, jt_code = _JOB_TYPE_MAP[jt_key]

    console.print(f"\n[yellow]How many jobs do you want to scrape and apply to? (To save API costs, e.g. 5 or 10. Press Enter for default: [bold]10[/bold]):[/yellow]")
    max_input = input("> ").strip()
    try:
        max_jobs = int(max_input) if max_input else 10
    except ValueError:
        max_jobs = 10

    console.print(f"\n[green]Selected domains    : {domains}[/green]")
    console.print(f"[green]City                : {final_city}[/green]")
    console.print(f"[green]Job type            : {jt_label}[/green]")
    console.print(f"[green]Job limit           : {max_jobs}[/green]")

    return {
        "confirmed_domains": domains,
        "confirmed_city": final_city,
        "job_type_code": jt_code,
        "job_type_label": jt_label,
        "max_jobs": max_jobs,
        "current_phase": "domains_confirmed"
    }
