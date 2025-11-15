# mcp_gmail_server.py
import base64
import os
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from fastmcp import FastMCP

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

app = FastMCP("gmail-mcp")

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

# ---------------------------------------------------------
#  GMAIL AUTH HANDLER  (OAuth)
# ---------------------------------------------------------
def get_gmail_service():
    """
    Loads token.json if it exists, otherwise triggers OAuth login.
    """
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_PATH}. "
                    "Download it from Google Cloud Console and place it there."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------
#  TOOL: List Messages
# ---------------------------------------------------------
@app.tool()
def list_messages(q: str = "is:unread", max_results: int = 10) -> dict:
    """
    List messages matching Gmail search query.
    Example queries:
      - is:unread
      - subject:Invoice
      - from:amazon.com
    """
    try:
        service = get_gmail_service()
        results = service.users().messages().list(
            userId="me", q=q, maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        return {"messages": messages}

    except HttpError as error:
        return {"error": str(error)}


# ---------------------------------------------------------
#  TOOL: Get Full Email
# ---------------------------------------------------------
@app.tool()
def get_message(id: str) -> dict:
    """
    Fetch a full Gmail message by ID.
    Decodes body + metadata.
    """
    try:
        service = get_gmail_service()
        msg = service.users().messages().get(
            userId="me", id=id, format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}

        # Extract plaintext body
        body = ""
        if "data" in msg["payload"]["body"]:
            body = base64.urlsafe_b64decode(
                msg["payload"]["body"]["data"]
            ).decode("utf-8", errors="ignore")
        else:
            # If multipart
            for part in msg["payload"].get("parts", []):
                if part["mimeType"] == "text/plain":
                    body = base64.urlsafe_b64decode(
                        part["body"]["data"]
                    ).decode("utf-8", errors="ignore")
                    break

        return {
            "id": msg["id"],
            "thread_id": msg.get("threadId"),
            "labels": msg.get("labelIds", []),
            "snippet": msg.get("snippet", ""),
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "received_at": headers.get("Date", ""),
            "body": body,
        }

    except HttpError as error:
        return {"error": str(error)}


# ---------------------------------------------------------
#  TOOL: Modify Labels
# ---------------------------------------------------------
@app.tool()
def modify_labels(
    id: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict:
    """
    Add/remove Gmail labels for a message.
    """
    try:
        service = get_gmail_service()

        body = {
            "addLabelIds": add_labels or [],
            "removeLabelIds": remove_labels or [],
        }

        result = service.users().messages().modify(
            userId="me",
            id=id,
            body=body
        ).execute()

        return {
            "id": id,
            "added": add_labels or [],
            "removed": remove_labels or [],
        }

    except HttpError as error:
        return {"error": str(error)}


# ---------------------------------------------------------
#  TOOL: Send Email (optional)
# ---------------------------------------------------------
@app.tool()
def send_email(to: str, subject: str, message: str) -> dict:
    """
    Optional helper tool to send Gmail emails.
    """
    try:
        service = get_gmail_service()

        mime_msg = MIMEText(message)
        mime_msg["to"] = to
        mime_msg["subject"] = subject

        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()

        result = service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()

        return {"message_id": result["id"]}

    except HttpError as error:
        return {"error": str(error)}


# ---------------------------------------------------------
#  START MCP SERVER
# ---------------------------------------------------------
if __name__ == "__main__":
    # Run as HTTP MCP server so you can call it via http://localhost:8001
    app.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8001,
        path="/mcp"
    )
