# LinkedIn Autonomous Application Agent

An end-to-end pipeline that reads your resume, scrapes LinkedIn for relevant openings, scores each job against your profile, and auto-fills Easy Apply forms — all without manual intervention.

Built with **LangGraph** for stateful orchestration, **Playwright** for stealth browser automation, and a multi-model **Groq LLM** stack for intelligent decision-making at every stage.

---

## Why I Built This

Applying to jobs on LinkedIn is repetitive and mentally exhausting. You search the same keywords, scroll through pages, click "Easy Apply," fill the same fields over and over, and half the listings aren't even relevant. I wanted to fix that.

This agent handles the full lifecycle: it reads a PDF resume to understand your skills, builds targeted search queries, scrapes job listings, uses LLM scoring to filter out bad matches, and then opens a real Chromium browser to fill and submit Easy Apply forms. The whole thing runs from a single CLI command.

---

## Architecture

The pipeline is a directed acyclic graph (DAG) built with LangGraph. Each node is an independently testable agent:

```
Resume Parser ──► Domain Confirm (CLI) ──► Scraper ──► Scorer ──► Applier ──► Logger
                                                         │
                                                   (Error Handler)
```

| Node | What it does |
|---|---|
| **Resume Parser** | Extracts name, email, skills, city, experience from a PDF using `pdfplumber` + LLM parsing |
| **Domain Confirm** | Interactive CLI prompt — pick from suggested job titles or type a custom search phrase |
| **Scraper** | Opens LinkedIn in Chromium, navigates to Jobs tab, runs filtered search, extracts job cards |
| **Scorer** | Sends each job description + your profile to an LLM, returns a 0–1 fit score with reasoning |
| **Applier** | Clicks Easy Apply, reads form fields, uses LLM to decide what to fill, handles dropdowns/text/uploads |
| **Logger** | Prints a run summary to CLI + optionally syncs applied jobs to Google Sheets |

### Error Recovery
Every node is wrapped in a `@resilient_node` decorator that:
- Logs timing and phase transitions
- Catches all exceptions and routes them to a dedicated error handler node instead of crashing
- Ensures the pipeline state is always consistent

---

## Multi-Model LLM Strategy

Different tasks need different model strengths. Instead of using one expensive model for everything, I set up role-based routing through Groq's API:

| Role | Primary Model | Fallback Chain | Why |
|---|---|---|---|
| **Scorer** | Llama 3.1 8B | → 20B → 70B | Speed matters here — scoring is high-volume, low-complexity |
| **Form Filler** | Llama 3.3 70B | → 120B → 20B | Needs reliable structured JSON output for field mapping |
| **Writer** | GPT-OSS 120B | → 70B → 20B | Cover letters and free-text answers need reasoning depth |
| **Locator** | Llama 3.1 8B | → 20B | CSS selector generation is trivial for small models |

If the primary model hits a rate limit, it automatically falls through to the next model in the chain. Gemini can be enabled as an override via `USE_GEMINI=true`.

---

## Browser Automation

The scraper and applier use [Patchright](https://github.com/nicegamer7/patchright) (a Playwright fork designed to evade bot detection):

- **Session persistence** — cookies stored locally so you don't re-login every run
- **Human-like delays** — randomized sleep intervals between every action (typing, clicking, scrolling)
- **Adaptive Locator** — when LinkedIn changes their DOM, the system captures the page HTML, simplifies it, asks the LLM for a new CSS selector, and caches it for future runs
- **Lazy-load handling** — scrolls the internal results container (not the window) to trigger LinkedIn's infinite scroll
- **Anti-detection** — custom user-agent, non-headless Chromium, no webdriver flags

### Form Filling Intelligence
The Easy Apply form filler doesn't just blindly map fields. It:
1. Reads the current step's DOM and extracts all visible form fields
2. Classifies each field as text input, dropdown (`<select>`), radio button, or file upload
3. For dropdowns, tries three strategies in order: value match → label match → JavaScript injection
4. Filters out placeholder options like "Select an option" before choosing
5. For experience-related dropdowns, picks the closest match to the user's actual years
6. Uploads a resume PDF when a file input is detected

---

## Setup

### Prerequisites
- Python 3.11+
- A LinkedIn account
- A [Groq API key](https://console.groq.com) (free tier works)

### Installation

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/Job_Scrapper_AI.git
cd Job_Scrapper_AI/linkedin_agent

# Create virtual environment
python -m venv venv
venv\Scripts\activate       # Windows
# source venv/bin/activate  # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
python -m playwright install chromium
```

### Configuration

```bash
# Copy the example and fill in your values
cp .env.example .env
```

Open `.env` and set:
- `GROQ_API_KEY` — from https://console.groq.com
- `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` — your LinkedIn login
- `RESUME_PATH` — absolute path to your resume PDF
- `SCORE_THRESHOLD` — minimum fit score (default `0.65`, meaning 65% match)

> **Google Sheets (optional):** Create a GCP service account, download the JSON key to `data/service_account.json`, and share your spreadsheet with the service account email. Set `SHEETS_ID` in `.env`. If you skip this, results are saved locally to SQLite.

---

## Usage

```bash
python main.py --resume "C:\path\to\your\resume.pdf"
```

The CLI will walk you through:

```
Suggested job domains from your resume:
  1. Data Analyst
  2. Business Intelligence Developer
  3. AI/ML Engineer
  ...

Type the exact job title or search phrase you want to use (e.g. Data Analyst Internship).
Or type the numbers of the suggested domains above (e.g. 1 or 1,3). Press Enter to select ALL:
> Data Analyst Internship

City for job search (detected: Bengaluru). Press Enter to confirm or type a new city:
>

What type of jobs are you targeting?
  1. Full-Time
  2. Part-Time
  3. Internship
  4. Contract
  5. Any (no filter)
> 3

How many jobs do you want to scrape and apply to? (Press Enter for default: 10):
> 5
```

The agent then opens a real Chromium window where you can watch it navigate, scrape, and apply.

### Run Summary

At the end of each run:
```
======================================
  Run Complete
======================================
  Scraped                   5
  Scored                    5
  Above threshold           3
  Applied                   3
  Manual review             0
  Skipped                   2
  Logged to Sheets          yes
======================================
```

---

## Project Structure

```
linkedin_agent/
├── main.py                  # Entry point — CLI args, pipeline init
├── config.py                # Loads .env, validates required keys
├── requirements.txt
├── .env.example
│
├── agents/
│   ├── resume_agent.py      # PDF parsing + domain suggestion + CLI prompts
│   ├── scraper_agent.py     # LinkedIn search + job card extraction
│   ├── scorer_agent.py      # LLM-based job-candidate fit scoring
│   ├── applier_agent.py     # Easy Apply form automation
│   └── logger_agent.py      # Run summary + optional Sheets sync
│
├── browser/
│   ├── browser_manager.py   # Playwright lifecycle, login, cookie mgmt
│   ├── adaptive_locator.py  # Self-healing CSS selectors via LLM
│   └── human_delay.py       # Randomized delay generator
│
├── graph/
│   ├── state.py             # TypedDict for LangGraph shared state
│   └── graph_builder.py     # DAG wiring: nodes, edges, error routing
│
├── utils/
│   ├── llm_client.py        # Multi-model Groq client with fallback chains
│   ├── decorators.py        # @resilient_node, @human_retry, @log_action
│   ├── dom_simplifier.py    # HTML → minimal structure for LLM context
│   ├── sheets_client.py     # Google Sheets API (service account auth)
│   └── logger.py            # SQLite-backed structured logging
│
├── db/
│   └── schema.py            # SQLite schema init (jobs, run_log, profile)
│
├── data/                    # Runtime data (gitignored)
│   ├── jobs.db
│   ├── linkedin_session.json
│   └── ui_patches.json
│
└── tests/
    └── test_phase5.py
```

---

## Key Design Decisions

**Why LangGraph over a simple script?**
The pipeline needs shared mutable state (profile, job list, scores) flowing through multiple phases with conditional error routing. LangGraph gives me that with typed state and compile-time graph validation — I don't need a full agent framework, just a robust DAG runner.

**Why Groq instead of OpenAI?**
Cost and speed. Groq's inference on Llama models is 10–20× faster than comparable API calls. The free tier is generous enough for daily use. The multi-model chain means I'm not locked into any single provider.

**Why Patchright over vanilla Playwright?**
LinkedIn's bot detection is aggressive. Vanilla Playwright gets flagged within minutes. Patchright patches the `navigator.webdriver` flag and other fingerprinting vectors out of the box.

**Why SQLite instead of just in-memory?**
If the pipeline crashes mid-run (browser timeout, LLM rate limit), I don't lose everything. On restart, the scraper checks the DB for already-processed URLs and skips them. The scorer only processes `status='scraped'` rows. This makes the whole system resumable.

---

## Limitations & Future Work

- **No external job boards** — currently LinkedIn only. Indeed/Glassdoor support is planned.
- **CAPTCHA handling** — if LinkedIn shows a CAPTCHA, the agent takes a screenshot and pauses. You resolve it manually, press Enter, and it continues.
- **Single browser session** — no parallelism. Running multiple instances on the same LinkedIn account will get flagged.
- **LinkedIn UI changes** — the Adaptive Locator mitigates this, but major layout overhauls may need manual selector updates.

---

## Tech Stack

| Component | Technology |
|---|---|
| Orchestration | LangGraph |
| LLMs | Groq (Llama 3.1 8B, Llama 3.3 70B, GPT-OSS 120B), Gemini (optional) |
| Browser | Patchright (Playwright fork) |
| Database | SQLite |
| PDF Parsing | pdfplumber |
| CLI UI | Rich |
| Logging | Google Sheets API (optional) |

---

## License

MIT
