# app/graph.py

import sqlite3
import logging
import json
from datetime import datetime, timedelta
from typing import Any, Dict

from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama

from app.state import EmailState
from app.tools.gmail_calendar_tools import (
    list_unread_emails,
    get_email,
    set_email_labels,
    create_calendar_block,
)

# -------------------------------------------------------------------
# Shared config
# -------------------------------------------------------------------
logging.basicConfig(
    filename="agent.log",           # log file next to your script
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

DB_PATH = "memory.db"


def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS emails (
            gmail_id TEXT PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            subject TEXT,
            snippet TEXT,
            body TEXT,
            received_at TEXT,
            labels TEXT,
            category TEXT,
            category_confidence REAL,
            last_updated_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()

llm = ChatOllama(model="qwen2.5:0.5b", temperature=0.1)


# -------------------------------------------------------------------
# Agent 1: Read Emails
# -------------------------------------------------------------------

def read_emails_node(state: EmailState) -> EmailState:
    """
    Read unread emails via MCP Gmail and store them in SQLite.
    """
    ensure_db() 

    raw_list = list_unread_emails()
    emails = []
    log.info(f"Raw list keys: {list(raw_list.keys())}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for msg in raw_list.get("messages", []):
        log.info(f"Reading message id={msg.get('id')}")
        gid = msg["id"]
        full = get_email(gid)
        log.info(f"Full email fields: {list(full.keys())}")

        subject = full.get("subject", "")
        snippet = full.get("snippet", "")
        body = full.get("body", "")
        received_at = full.get("received_at")  # map fields as per tool
        labels = full.get("labels", [])

        cur.execute(
            """
            INSERT OR IGNORE INTO emails
            (gmail_id, thread_id, from_addr, to_addr, subject, snippet, body,
             received_at, labels, category, category_confidence, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                gid,
                full.get("thread_id"),
                full.get("from", ""),
                full.get("to", ""),
                subject,
                snippet,
                body,
                received_at,
                json.dumps(labels),
                datetime.utcnow().isoformat(),
            ),
        )

        emails.append(full)

    conn.commit()
    conn.close()

    state["emails"] = emails
    state["current_email_index"] = 0
    return state


# -------------------------------------------------------------------
# Agent 2: Categorizer
# -------------------------------------------------------------------

CATEGORIZE_SYSTEM = """You are classifying emails into EXACTLY ONE of these categories:

1. urgent_action
   - Requires Vinod or Shilpa to personally DO something within the next 48 hours.
   - Examples: bills due, invoice/payment required, meeting to confirm, form to sign, reply requested, school/medical updates needing a response, travel changes.

2. newsletter
   - Marketing, promotions, sales, shopping offers, “Black Friday”, coupons.
   - Recurring newsletters or product updates.
   - Time-limited sales still go here UNLESS it's clearly a bill or an existing subscription charge.

3. weekend_reading
   - Long articles, blog posts, tutorials, webinars, course content.
   - Interesting but no direct action required soon.

4. ignore
   - Obvious spam, junk, or irrelevant things.

VERY IMPORTANT:
- Promotional shopping emails (Amazon, Target, clothing brands, etc.) are **NOT** urgent_action.
- If you are unsure, choose **newsletter**, NOT urgent_action.

Return ONLY the category name as plain text: one of:
urgent_action, newsletter, weekend_reading, ignore.
"""

ALLOWED_CATEGORIES = {
    "urgent_action",
    "newsletter",
    "ignore",
    "weekend_reading",
}

def _extract_category(raw: str) -> str:
    """
    Take the raw LLM output and map it to one of the allowed categories.
    Very tolerant: looks for known keywords inside the text.
    """
    text = (raw or "").strip().lower()

    # exact match first
    if text in ALLOWED_CATEGORIES:
        return text

    # handle formats like "category: urgent_action, ads"
    for cat in ALLOWED_CATEGORIES:
        if cat in text:
            return cat

    # last resort
    return "weekend_reading"


def categorize_emails_node(state: EmailState) -> EmailState:
    log.info("ENTER: categorize_emails_node")

    emails = state.get("emails", []) or []
    log.info(f"Categorizing {len(emails)} emails")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    updated_count = 0

    for e in emails:
        eid = e.get("id")
        subject = e.get("subject")
        body = (e.get("body") or "")[:4000]
        from_addr = e.get("from")

        log.info(f"[categorize] Processing gmail_id={eid} subject={subject!r}")

        content = f"From: {from_addr}\nSubject: {subject}\nBody:\n{body}"

        try:
            resp = llm.invoke(
                [
                    {"role": "system", "content": CATEGORIZE_SYSTEM},
                    {"role": "user", "content": content},
                ]
            )
            raw = resp.content if hasattr(resp, "content") else str(resp)
            log.info(f"[categorize] LLM raw response: {raw!r}")
        except Exception as ex:
            log.error(f"[categorize] LLM error for {eid}: {ex}")
            raw = ""

        cat = _extract_category(raw)
        log.info(f"[categorize] Final category for {eid}: {cat!r}")

        try:
            cur.execute(
                """
                UPDATE emails
                   SET category = ?,
                       category_confidence = ?,
                       last_updated_at = ?
                 WHERE gmail_id = ?
                """,
                (cat, 0.7, datetime.utcnow().isoformat(), eid),
            )
            rowcount = cur.rowcount
            updated_count += rowcount
            log.info(
                f"[categorize] UPDATE emails SET category={cat!r} "
                f"WHERE gmail_id={eid!r} → rowcount={rowcount}"
            )
        except Exception as ex:
            log.error(f"[categorize] DB UPDATE error for {eid}: {ex}")

    conn.commit()
    conn.close()
    log.info(f"EXIT: categorize_emails_node updated_count={updated_count}")

    return state

# -------------------------------------------------------------------
# Agent 3: Organizer (apply Gmail labels)
# -------------------------------------------------------------------

CATEGORY_LABEL_MAP = {
    "urgent_action": "AI/Urgent",
    "ads": "AI/Ads",
    "awaiting_reply": "AI/AwaitingReply",
    "personal": "AI/Personal",
    "weekend_reading": "AI/WeekendReading",
}


def organize_emails_node(state: EmailState) -> EmailState:
    log.info("ENTER: organize_emails_node")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT gmail_id, category FROM emails WHERE category IS NOT NULL"
    ).fetchall()

    for gmail_id, cat in rows:
        label = CATEGORY_LABEL_MAP.get(cat)
        if not label:
            continue

        log.info(f"[organize] Setting label {label!r} for {gmail_id}")
        resp = set_email_labels(gmail_id, add_labels=[label])
        if isinstance(resp, dict) and "error" in resp:
            log.error(f"[organize] Label update failed for {gmail_id}: {resp['error']}")

    conn.close()
    log.info("EXIT: organize_emails_node")
    return state



# -------------------------------------------------------------------
# Agent 4: Scheduler (Calendar blocks)
# -------------------------------------------------------------------

def scheduler_node(state: EmailState) -> EmailState:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    urgent = cur.execute(
        "SELECT subject, gmail_id FROM emails WHERE category='urgent_action'"
    ).fetchall()
    weekend = cur.execute(
        "SELECT subject, gmail_id FROM emails WHERE category='weekend_reading'"
    ).fetchall()

    now = datetime.now()

    # Urgent: block today/tomorrow (simple: 2 hours from now, 30 min)
    for subj, gid in urgent:
        start = now + timedelta(hours=2)
        end = start + timedelta(minutes=30)
        create_calendar_block(
            summary=f"Process urgent email: {subj}",
            start_iso=start.isoformat(),
            end_iso=end.isoformat(),
        )

    # Weekend reading: Saturday at 10 AM (next Saturday)
    if weekend:
        next_saturday = now + timedelta((5 - now.weekday()) % 7)
        weekend_start = next_saturday.replace(hour=10, minute=0, second=0, microsecond=0)
        weekend_end = weekend_start + timedelta(hours=1)

        for subj, gid in weekend:
            create_calendar_block(
                summary=f"Weekend reading: {subj}",
                start_iso=weekend_start.isoformat(),
                end_iso=weekend_end.isoformat(),
            )

    conn.close()
    return state


# -------------------------------------------------------------------
# Agent 5: Validator
# -------------------------------------------------------------------

VALIDATOR_SYSTEM_PROMPT = """
You are validating email triage categories.

Allowed categories:
1. urgent_action – important and needs immediate attention.
2. ads – advertisements, marketing, newsletters with no action.
3. awaiting_reply – not urgent but awaiting my reply or follow-up.
4. personal – personal, family, or friends.
5. weekend_reading – interesting but no urgency, fine to read on the weekend.

You will receive:
- the current category assigned by a previous agent
- the email (subject, snippet, body)

You MUST respond with STRICT JSON only, no extra text:

If the current category is correct:
{
  "keep": true,
  "new_category": null,
  "reason": "short explanation"
}

If it is wrong:
{
  "keep": false,
  "new_category": "<one_of_allowed>",
  "reason": "short explanation"
}
"""


def _safe_parse_json(text: str) -> Dict[str, Any]:
    """
    Tries to parse JSON from the model output.
    Falls back to a default 'keep' if parsing fails.
    """
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
    return {"keep": True, "new_category": None, "reason": "fallback_keep"}


def validator_node(state: EmailState) -> EmailState:
    """
    Iterate through categorized emails and let the LLM
    confirm or correct the category. If corrected, update DB and Gmail labels.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT gmail_id, subject, snippet, body, category
        FROM emails
        WHERE category IS NOT NULL
        ORDER BY last_updated_at DESC
        LIMIT 20
        """
    ).fetchall()

    notes = state.get("notes", "")

    for gmail_id, subject, snippet, body, category in rows:
        body = body or ""
        snippet = snippet or ""
        current_cat = category or ""

        email_text = (
            f"Current category: {current_cat}\n\n"
            f"Subject: {subject}\n"
            f"Snippet: {snippet}\n\n"
            f"Body:\n{body[:4000]}"
        )

        resp = llm.invoke(
            [
                {"role": "system", "content": VALIDATOR_SYSTEM_PROMPT},
                {"role": "user", "content": email_text},
            ]
        )

        raw_text = resp.content if hasattr(resp, "content") else str(resp)
        parsed = _safe_parse_json(raw_text)

        keep = bool(parsed.get("keep", True))
        new_category = parsed.get("new_category")
        reason = parsed.get("reason", "")

        if keep or not new_category:
            cur.execute(
                """
                UPDATE emails
                   SET category_confidence = ?,
                       last_updated_at = ?
                 WHERE gmail_id = ?
                """,
                (0.9, datetime.utcnow().isoformat(), gmail_id),
            )
            notes += f"\n[VALIDATOR] Kept category '{current_cat}' for {gmail_id}: {reason}"
            continue

        new_category = str(new_category).strip()
        if new_category not in ALLOWED_CATEGORIES:
            notes += (
                f"\n[VALIDATOR] Ignored unknown new_category '{new_category}' "
                f"for {gmail_id}, keeping '{current_cat}'."
            )
            continue

        cur.execute(
            """
            UPDATE emails
               SET category = ?,
                   category_confidence = ?,
                   last_updated_at = ?
             WHERE gmail_id = ?
            """,
            (new_category, 0.85, datetime.utcnow().isoformat(), gmail_id),
        )

        old_label = CATEGORY_LABEL_MAP.get(current_cat)
        new_label = CATEGORY_LABEL_MAP.get(new_category)

        add_labels = [new_label] if new_label else []
        remove_labels = [old_label] if old_label else []

        try:
            if add_labels or remove_labels:
                set_email_labels(
                    gmail_id,
                    add_labels=add_labels,
                    remove_labels=remove_labels,
                )
        except Exception as e:
            notes += f"\n[VALIDATOR] Failed label sync for {gmail_id}: {e}"

        notes += (
            f"\n[VALIDATOR] Updated {gmail_id}: '{current_cat}' → '{new_category}' "
            f"({reason})"
        )

    conn.commit()
    conn.close()

    state["notes"] = notes
    return state


# -------------------------------------------------------------------
# LangGraph wiring
# -------------------------------------------------------------------

def build_app():
    """
    Build and compile the LangGraph app that wires all agents together.
    """
    ensure_db()
    graph = StateGraph(EmailState)

    graph.add_node("read_emails", read_emails_node)
    graph.add_node("categorize", categorize_emails_node)
    graph.add_node("organize", organize_emails_node)
    graph.add_node("schedule", scheduler_node)
    graph.add_node("validate", validator_node)

    graph.set_entry_point("read_emails")

    graph.add_edge("read_emails", "categorize")
    graph.add_edge("categorize", "organize")
    graph.add_edge("organize", "schedule")
    graph.add_edge("schedule", "validate")
    graph.add_edge("validate", END)

    return graph.compile()
