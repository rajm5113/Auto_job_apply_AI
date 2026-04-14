import sqlite3
import os

def init_db(db_path: str):
    dir_path = os.path.dirname(db_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id             TEXT PRIMARY KEY,
            job_title      TEXT,
            company        TEXT,
            location       TEXT,
            job_url        TEXT UNIQUE,
            description    TEXT,
            easy_apply     INTEGER DEFAULT 1,
            score          REAL,
            reasons        TEXT,
            missing_skills TEXT,
            status         TEXT DEFAULT 'scraped',
            logged         INTEGER DEFAULT 0,
            scraped_at     TEXT,
            applied_at     TEXT,
            fail_reason    TEXT
        );

        CREATE TABLE IF NOT EXISTS user_profile (
            id           INTEGER PRIMARY KEY DEFAULT 1,
            raw_text     TEXT,
            profile_json TEXT,
            created_at   TEXT,
            updated_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id  TEXT,
            phase   TEXT,
            message TEXT,
            level   TEXT,
            ts      TEXT
        );
    """)

    conn.commit()
    conn.close()
