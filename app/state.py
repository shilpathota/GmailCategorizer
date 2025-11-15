from typing import TypedDict, List, Optional, Dict, Any

class EmailState(TypedDict):
    emails: List[Dict[str, Any]]  # list of emails pulled from DB/MCP
    current_email_index: int
    notes: str
