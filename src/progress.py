"""
User progress persistence: saves and loads per-user, per-cert history as JSON.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, List

from src.loader import cert_users_dir


def _user_path(cert: str, username: str) -> str:
    safe = username.replace(" ", "_").lower()
    users_dir = cert_users_dir(cert)
    return os.path.join(users_dir, f"{safe}.json")


def _load_raw(cert: str, username: str) -> dict:
    path = _user_path(cert, username)
    if not os.path.exists(path):
        return {"username": username, "cert": cert, "sessions": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_session(cert: str, username: str, summary: dict) -> None:
    """Append a completed session summary to the user's history file."""
    users_dir = cert_users_dir(cert)
    os.makedirs(users_dir, exist_ok=True)
    data = _load_raw(cert, username)
    data["sessions"].append({
        "date": datetime.now().isoformat(timespec="seconds"),
        **summary,
    })
    path = _user_path(cert, username)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def load_history(cert: str, username: str) -> List[dict]:
    """Return list of past session summaries."""
    return _load_raw(cert, username).get("sessions", [])


def weak_questions(cert: str, username: str) -> Dict[str, int]:
    """Return {question_id: wrong_count} sorted by most wrong."""
    counts: Dict[str, int] = {}
    for session in load_history(cert, username):
        for qid in session.get("wrong_ids", []):
            counts[qid] = counts.get(qid, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))
