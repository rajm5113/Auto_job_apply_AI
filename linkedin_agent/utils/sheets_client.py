"""
Google Sheets client — uses a Service Account for zero-friction authentication.
No browser popup, no OAuth flow, no token.json needed.

Setup (one time):
  1. In Google Cloud Console → IAM & Admin → Service Accounts → Create Service Account
  2. Create a JSON key for it → download → rename to service_account.json → put in data/
  3. Share your Google Sheet with the service account email (Editor access)
"""

import json
import os
from rich.console import Console
import config

console = Console()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_PATH = os.path.join(os.path.dirname(config.DB_PATH),
                                     "service_account.json")


class SheetsClient:
    def __init__(self):
        self._service = None

    def authenticate(self) -> "SheetsClient":
        """Build the Sheets API service using the Service Account key file."""
        if not os.path.exists(SERVICE_ACCOUNT_PATH):
            raise FileNotFoundError(
                f"Service account key not found at: {SERVICE_ACCOUNT_PATH}\n"
                "Please follow the setup instructions in sheets_client.py"
            )

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_PATH, scopes=SCOPES
            )
            self._service = build("sheets", "v4", credentials=creds)
            console.print("[dim]Google Sheets: authenticated via service account.[/dim]")
        except ImportError:
            raise ImportError(
                "Google API packages not installed. Run:\n"
                "  pip install google-auth google-auth-httplib2 google-api-python-client"
            )
        return self

    def batch_append(self, spreadsheet_id: str, tab_name: str, rows: list[list]):
        """
        Append a list of rows to the given tab.
        Each row is a list of cell values, e.g. ["Job Title", "Company", "URL"].
        Skips silently if rows is empty.
        """
        if not rows:
            return

        if self._service is None:
            raise RuntimeError("Call authenticate() before batch_append().")

        body = {"values": rows}
        self._service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()

        console.print(
            f"[dim]Sheets: wrote {len(rows)} row(s) → [{tab_name}][/dim]"
        )
