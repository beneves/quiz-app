"""
Data models for CCNA Quiz questions and user sessions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class QuestionType(str, Enum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    EQUIVALENCE = "equivalence_buttons"
    DRAG_DROP = "drag_drop"
    SIMULATION_LAB = "simulation_lab"


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class Mode(str, Enum):
    STUDY = "study"
    EXAM = "exam"


@dataclass
class Topology:
    type: str  # "ascii" or "image"
    ascii_diagram: Optional[str] = None
    image_path: Optional[str] = None


@dataclass
class SimulationLab:
    intro: str = ""
    objectives: List[str] = field(default_factory=list)
    initial_prompt: str = "Device#"
    image: str = ""
    required_commands: List[str] = field(default_factory=list)
    verification_commands: List[str] = field(default_factory=list)
    command_responses: Dict[str, str] = field(default_factory=dict)


@dataclass
class Question:
    id: str
    topic: str
    subtopic: str
    difficulty: Difficulty
    type: QuestionType
    question: str
    tags: List[str] = field(default_factory=list)
    # single / multiple choice
    options: Optional[Dict[str, str]] = None
    correct_answer: Optional[List[str]] = None
    # equivalence
    left_items: Optional[Dict[str, str]] = None
    right_items: Optional[Dict[str, str]] = None
    correct_matches: Optional[Dict[str, str]] = None
    # shared
    explanation: str = ""
    exam_tip: str = ""
    source: str = ""
    exhibit: str = ""
    topology: Optional[Topology] = None
    lab: Optional[SimulationLab] = None

    def is_correct(self, answer) -> bool:
        if self.type == QuestionType.SINGLE_CHOICE:
            return isinstance(answer, str) and answer.upper() == self.correct_answer[0].upper()
        if self.type == QuestionType.MULTIPLE_CHOICE:
            if not isinstance(answer, (list, set)):
                return False
            return set(a.upper() for a in answer) == set(a.upper() for a in self.correct_answer)
        if self.type in (QuestionType.EQUIVALENCE, QuestionType.DRAG_DROP):
            if not isinstance(answer, dict):
                return False
            return all(
                answer.get(k, "").upper() == v.upper()
                for k, v in self.correct_matches.items()
            )
        if self.type == QuestionType.SIMULATION_LAB:
            if not isinstance(answer, dict) or not self.lab:
                return False
            issued = set(str(cmd).strip().lower() for cmd in answer.get("commands", []))
            required = set(self.lab.required_commands)
            verification = set(self.lab.verification_commands)
            return required.issubset(issued) and verification.issubset(issued)
        return False


@dataclass
class SessionResult:
    question_id: str
    correct: bool
    user_answer: object


@dataclass
class QuizSession:
    mode: Mode
    questions: List[Question]
    results: List[SessionResult] = field(default_factory=list)
    current_index: int = 0

    @property
    def current_question(self) -> Optional[Question]:
        if self.current_index < len(self.questions):
            return self.questions[self.current_index]
        return None

    @property
    def is_finished(self) -> bool:
        return self.current_index >= len(self.questions)

    @property
    def score(self) -> int:
        return sum(1 for r in self.results if r.correct)

    def record(self, answer) -> bool:
        q = self.current_question
        if q is None:
            return False
        correct = q.is_correct(answer)
        self.results.append(SessionResult(q.id, correct, answer))
        self.current_index += 1
        return correct
