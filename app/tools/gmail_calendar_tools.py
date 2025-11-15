# tools/gmail_calendar_tools.py
from .mcp_client import call_tool

def list_unread_emails():
    return call_tool("list_messages", {"q": "is:unread"})

def get_email(gmail_id: str):
    return call_tool("get_message", {"id": gmail_id})

def set_email_labels(gmail_id: str, add_labels: list[str], remove_labels: list[str] | None = None):
    return call_tool("modify_labels", {
        "id": gmail_id,
        "add_labels": add_labels,
        "remove_labels": remove_labels or [],
    })

def create_calendar_block(summary: str, start_iso: str, end_iso: str):
    return call_tool("create_event", {
        "summary": summary,
        "start": start_iso,
        "end": end_iso,
    })
