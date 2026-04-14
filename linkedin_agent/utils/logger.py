import sqlite3
from datetime import datetime
from rich.console import Console

class Logger:
    def __init__(self, db_path: str, run_id: str):
        self.run_id = run_id
        self.db_path = db_path
        self.console = Console()

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _write(self, phase: str, msg: str, level: str, color: str):
        self.console.print(f"[{color}][{self._ts()}] [{phase}] {msg}[/{color}]")
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO run_log (run_id, phase, message, level, ts) VALUES (?,?,?,?,?)",
                (self.run_id, phase, msg, level, datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def info(self, phase: str, msg: str):
        self._write(phase, msg, "INFO", "green")

    def warn(self, phase: str, msg: str):
        self._write(phase, msg, "WARN", "yellow")

    def error(self, phase: str, msg: str):
        self._write(phase, msg, "ERROR", "red")
