"""
Quiz engine: orchestrates a session, exposes the current question,
accepts answers and advances state. Decoupled from the UI.
"""
from __future__ import annotations

from typing import List

from src.models import Mode, Question, QuizSession


def submit_answer(session: QuizSession, answer) -> bool:
    """
    Submit an answer for the current question.
    Returns True if correct, False otherwise.
    Advances the session to the next question.
    """
    return session.record(answer)


def summary(session: QuizSession) -> dict:
    """Return a dict with session statistics."""
    total = len(session.results)
    correct = session.score
    wrong_ids = [r.question_id for r in session.results if not r.correct]
    return {
        "total": total,
        "correct": correct,
        "wrong": total - correct,
        "score_pct": round(correct / total * 100, 1) if total else 0.0,
        "wrong_ids": wrong_ids,
    }
