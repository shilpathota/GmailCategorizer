# main.py
import argparse
import logging
from datetime import datetime

from app.graph import build_app
from app.state import EmailState


def run_triage(mode: str = "full"):
    """
    Run one triage cycle:
    - read emails via MCP Gmail
    - categorize with LLM
    - apply labels via MCP
    - block time on calendar
    - validate categories
    """
    app = build_app()

    initial_state: EmailState = {
        "emails": [],
        "current_email_index": 0,
        "notes": f"run started at {datetime.utcnow().isoformat()}",
    }

    # If later you want streaming, you can use app.astream(), for now invoke is fine
    final_state = app.invoke(initial_state)

    print("✅ Triage run completed.")
    print(f"Notes: {final_state.get('notes', '')}")


def main():
    parser = argparse.ArgumentParser(description="AI Inbox Agent (LangGraph + MCP)")
    parser.add_argument(
        "command",
        nargs="?",
        default="triage",
        choices=["triage"],
        help="What to do. For now only 'triage' is supported.",
    )
    parser.add_argument(
        "--mode",
        default="full",
        choices=["full"],
        help="Future: run subsets (e.g., categorize-only, schedule-only).",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.command == "triage":
        run_triage(mode=args.mode)
    else:
        raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
