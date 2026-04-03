"""
YAML question loader with validation and duplicate-ID detection.
Supports multiple certification folders under data/.
"""
from __future__ import annotations

import glob
import os
from typing import Dict, List, Optional

import yaml

from src.models import Difficulty, Question, QuestionType, SimulationLab, Topology

DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")


def available_certs(data_root: str = DATA_ROOT) -> List[str]:
    """Return sorted list of certification folder names found under data/."""
    data_root = os.path.abspath(data_root)
    if not os.path.isdir(data_root):
        return []
    return sorted(
        d for d in os.listdir(data_root)
        if os.path.isdir(os.path.join(data_root, d))
        and not d.startswith(".")
    )


def cert_questions_dir(cert: str, data_root: str = DATA_ROOT) -> str:
    return os.path.join(os.path.abspath(data_root), cert, "questions")


def cert_users_dir(cert: str, data_root: str = DATA_ROOT) -> str:
    return os.path.join(os.path.abspath(data_root), cert, "users")


def _normalize_command(cmd: str) -> str:
    return " ".join(str(cmd or "").strip().lower().split())


# ?? parsers ?????????????????????????????????????????????????????????????????

def _parse_topology(raw: Optional[dict]) -> Optional[Topology]:
    if not raw:
        return None
    return Topology(
        type=raw.get("type", "ascii"),
        ascii_diagram=raw.get("ascii_diagram"),
        image_path=raw.get("image_path"),
    )


def _parse_lab(raw: Optional[dict]) -> Optional[SimulationLab]:
    if not raw:
        return None
    responses = {
        _normalize_command(k): str(v)
        for k, v in (raw.get("command_responses") or {}).items()
    }
    return SimulationLab(
        intro=raw.get("intro", ""),
        objectives=[str(x) for x in (raw.get("objectives") or [])],
        initial_prompt=raw.get("initial_prompt", "Device#"),
        image=raw.get("image", ""),
        required_commands=[_normalize_command(x) for x in (raw.get("required_commands") or [])],
        verification_commands=[_normalize_command(x) for x in (raw.get("verification_commands") or [])],
        command_responses=responses,
    )


def _legacy_simulation_to_lab(raw: dict) -> Optional[SimulationLab]:
    requirements = [str(x) for x in (raw.get("requirements") or [])]
    solution = raw.get("solution") or {}
    required_commands = []
    responses = {}
    if isinstance(solution, dict):
        for device, commands in solution.items():
            header = f"! {device}"
            responses[_normalize_command(header)] = "Entering device context is conceptual in this lab."
            for cmd in commands or []:
                normalized = _normalize_command(cmd)
                if normalized:
                    required_commands.append(normalized)
                    responses.setdefault(normalized, "OK")
    elif isinstance(solution, list):
        for cmd in solution:
            normalized = _normalize_command(cmd)
            if normalized:
                required_commands.append(normalized)
                responses.setdefault(normalized, "OK")
    if not requirements and not required_commands and not responses:
        return None
    return SimulationLab(
        intro="Legacy simulation converted to Telegram lab mode.",
        objectives=requirements,
        initial_prompt="Device#",
        image="",
        required_commands=required_commands,
        verification_commands=[],
        command_responses=responses,
    )


def _parse_drag_drop(raw: dict) -> tuple[Optional[Dict[str, str]], Optional[Dict[str, str]], Optional[Dict[str, str]]]:
    pairs = raw.get("pairs") or {}
    left_items: Dict[str, str] = {}
    right_items: Dict[str, str] = {}
    correct_matches: Dict[str, str] = {}
    right_lookup: Dict[str, str] = {}
    left_index = 1
    right_index = 1
    for left_text, right_value in pairs.items():
        if isinstance(right_value, list):
            right_label = str(left_text)
            rkey = right_lookup.setdefault(right_label, f"R{right_index}")
            if rkey == f"R{right_index}":
                right_items[rkey] = right_label
                right_index += 1
            for item in right_value:
                lkey = f"L{left_index}"
                left_index += 1
                left_items[lkey] = str(item)
                correct_matches[lkey] = rkey
        else:
            lkey = f"L{left_index}"
            left_index += 1
            left_items[lkey] = str(left_text)
            right_label = str(right_value)
            rkey = right_lookup.get(right_label)
            if not rkey:
                rkey = f"R{right_index}"
                right_index += 1
                right_lookup[right_label] = rkey
                right_items[rkey] = right_label
            correct_matches[lkey] = rkey
    return left_items or None, right_items or None, correct_matches or None


def _parse_question(raw: dict) -> Question:
    raw_type = str(raw["type"])
    if raw_type == "drag_drop":
        qtype = QuestionType.EQUIVALENCE
    elif raw_type == "simulation":
        qtype = QuestionType.SIMULATION_LAB
    else:
        qtype = QuestionType(raw_type)
    difficulty = Difficulty(raw.get("difficulty", "medium"))

    correct_raw = raw.get("correct_answer")
    if correct_raw is None:
        correct_answer = None
    elif isinstance(correct_raw, list):
        correct_answer = [str(c) for c in correct_raw]
    else:
        correct_answer = [str(correct_raw)]

    lab = _parse_lab(raw.get("lab"))
    if qtype == QuestionType.SIMULATION_LAB and lab is None:
        lab = _legacy_simulation_to_lab(raw)
    if qtype == QuestionType.SIMULATION_LAB and lab is None:
        raise ValueError("simulation_lab questions require a 'lab' section")

    left_items = raw.get("left_items")
    right_items = raw.get("right_items")
    correct_matches = raw.get("correct_matches")
    if raw_type == "drag_drop":
        left_items, right_items, correct_matches = _parse_drag_drop(raw)

    return Question(
        id=str(raw["id"]),
        topic=raw["topic"],
        subtopic=raw.get("subtopic", ""),
        difficulty=difficulty,
        type=qtype,
        question=raw["question"],
        tags=raw.get("tags") or [],
        options=raw.get("options"),
        correct_answer=correct_answer,
        left_items=left_items,
        right_items=right_items,
        correct_matches=correct_matches,
        explanation=raw.get("explanation", ""),
        exam_tip=raw.get("exam_tip", ""),
        source=raw.get("source", ""),
        exhibit=raw.get("exhibit", ""),
        topology=_parse_topology(raw.get("topology")),
        lab=lab,
    )


# ?? public API ??????????????????????????????????????????????????????????????

def load_questions(cert: str, data_root: str = DATA_ROOT) -> List[Question]:
    """
    Load all YAML files for a given certification.
    Raises ValueError on schema errors or duplicate IDs.
    """
    questions: List[Question] = []
    seen_ids: Dict[str, str] = {}

    questions_dir = cert_questions_dir(cert, data_root)
    pattern = os.path.join(questions_dir, "**", "*.yaml")
    files = sorted(glob.glob(pattern, recursive=True))
    if not files:
        files = sorted(glob.glob(os.path.join(questions_dir, "*.yaml")))

    for filepath in files:
        with open(filepath, encoding="utf-8") as fh:
            content = fh.read()
        raw_list = []
        for doc in yaml.safe_load_all(content):
            if not doc:
                continue
            if isinstance(doc, list):
                raw_list.extend(doc)
            elif isinstance(doc, dict):
                raw_list.append(doc)
        if not raw_list:
            continue
        for raw in raw_list:
            try:
                q = _parse_question(raw)
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"Error in {filepath} id={raw.get('id', '?')}: {exc}"
                ) from exc
            if q.id in seen_ids:
                raise ValueError(
                    f"Duplicate ID '{q.id}' in {filepath} "
                    f"(first seen in {seen_ids[q.id]})"
                )
            seen_ids[q.id] = filepath
            questions.append(q)

    return questions


def available_topics(questions: List[Question]) -> List[str]:
    """Return topics in the order they first appear in the question list (load order)."""
    seen = []
    for q in questions:
        if q.topic not in seen:
            seen.append(q.topic)
    return seen


def available_question_types(questions: List[Question]) -> List[str]:
    """Return question types in first-seen order."""
    seen = []
    for q in questions:
        qtype = str(q.type.value if isinstance(q.type, QuestionType) else q.type)
        if qtype not in seen:
            seen.append(qtype)
    return seen


def build_seq_map(questions: List[Question]) -> Dict[str, int]:
    """Return {question_id: sequential_number} in load order (1-based)."""
    return {q.id: i + 1 for i, q in enumerate(questions)}


def build_topic_ranges(questions: List[Question]) -> Dict[str, tuple]:
    """
    Return {topic: (first_seq, last_seq, count)} based on load order.
    Sequential numbers are 1-based and reflect the order questions were loaded.
    """
    ranges: Dict[str, list] = {}
    for i, q in enumerate(questions):
        seq = i + 1
        if q.topic not in ranges:
            ranges[q.topic] = [seq, seq, 1]
        else:
            entry = ranges[q.topic]
            entry[1] = max(entry[1], seq)
            entry[2] += 1
    return {t: (v[0], v[1], v[2]) for t, v in ranges.items()}
