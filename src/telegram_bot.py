"""
Telegram bot — Quiz-App.

Flow:
  /start → cert → mode →
    Exam:  topic_screen → count_screen → quiz (shuffled + timer) → results (saved)
    Study: topic_screen → study_select  → quiz (ordered)         → results (not saved)

All interaction is button-only (no free-text input required).
"""
from __future__ import annotations

import html
import logging
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.loader import (
    available_certs,
    available_topics,
    build_topic_ranges,
    load_questions,
)
from src.models import Mode, Question, QuestionType, QuizSession
from src.progress import load_history, save_session
from src.quiz_engine import submit_answer, summary
from src.topology_renderer import render as render_topology

log = logging.getLogger(__name__)

PASS_THRESHOLD = 80  # percent
EXAM_COUNTS    = [10, 50, 70, 100, 120]


# ── helpers ───────────────────────────────────────────────────────────────────

def esc(text) -> str:
    return html.escape(str(text or ""))


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
         for row in rows]
    )


def _ud(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data


def _session(context: ContextTypes.DEFAULT_TYPE) -> QuizSession:
    return _ud(context)["session"]


async def _edit(update: Update, text: str, markup=None, parse_mode: str = "HTML") -> None:
    q = update.callback_query
    try:
        if q:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
        else:
            await update.effective_message.reply_text(
                text, reply_markup=markup, parse_mode=parse_mode
            )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _nav(back_label: str = "🔙 Back") -> list[tuple[str, str]]:
    return [(back_label, "nav_back"), ("🏠 Home", "nav_home")]


def _cert_icon(cert: str) -> str:
    for key, icon in {"CCNA": "🌐", "CCNP": "🌐", "CompTIA": "💻",
                      "AWS": "☁️", "Azure": "☁️", "GCP": "☁️"}.items():
        if key in cert:
            return icon
    return "📘"


def _fmt_time(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s"


def _normalize_lab_command(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _question_image_path(cert: str, q: Question) -> str | None:
    if q.type != QuestionType.SIMULATION_LAB:
        return None
    candidate = (q.lab.image if q.lab and q.lab.image else q.source).strip()
    if not candidate or not candidate.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
        return None
    path = os.path.join(ROOT, "data", cert, "images", candidate)
    return path if os.path.isfile(path) else None


async def _maybe_send_lab_image(update: Update, context: ContextTypes.DEFAULT_TYPE, q: Question) -> None:
    cert = _ud(context).get("cert", "")
    path = _question_image_path(cert, q)
    if not path:
        return
    ud = _ud(context)
    if ud.get("lab_image_for") == q.id:
        return
    target = update.callback_query.message if update.callback_query else update.effective_message
    if target is None:
        return
    with open(path, "rb") as fh:
        await target.reply_photo(photo=fh, caption=f"{q.id} lab image")
    ud["lab_image_for"] = q.id


def _ensure_lab_state(ud: dict, q: Question) -> dict:
    state = ud.get("lab_state")
    if state and state.get("question_id") == q.id:
        return state
    prompt = (q.lab.initial_prompt if q.lab and q.lab.initial_prompt else "Device#").strip()
    state = {
        "question_id": q.id,
        "prompt": prompt,
        "normalized_history": [],
        "normalized_set": set(),
        "raw_history": [],
    }
    ud["lab_state"] = state
    return state


def _lab_known_commands(q: Question) -> set[str]:
    if not q.lab:
        return set()
    return set(q.lab.required_commands) | set(q.lab.verification_commands) | set(q.lab.command_responses) | {"configure terminal", "conf t", "end", "exit"}


def _lab_missing_commands(q: Question, state: dict) -> tuple[list[str], list[str]]:
    if not q.lab:
        return [], []
    issued = state.get("normalized_set", set())
    missing_required = [cmd for cmd in q.lab.required_commands if cmd not in issued]
    missing_verify = [cmd for cmd in q.lab.verification_commands if cmd not in issued]
    return missing_required, missing_verify


def _next_lab_prompt(current_prompt: str, normalized: str, default_prompt: str) -> str:
    hostname = default_prompt.split("(")[0].rstrip("#") or "Device"
    if normalized in {"configure terminal", "conf t"}:
        return f"{hostname}(config)#"
    if normalized.startswith("interface "):
        return f"{hostname}(config-if)#"
    if normalized == "exit":
        if "(config-if)" in current_prompt:
            return f"{hostname}(config)#"
        return f"{hostname}#"
    if normalized == "end":
        return f"{hostname}#"
    return current_prompt or default_prompt


# ── screen renderers ──────────────────────────────────────────────────────────

async def render_cert_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = _ud(context)
    ud["state"] = "cert"
    certs = available_certs()
    if not certs:
        await _edit(
            update,
            "⚠️ No certification folders found in <code>data/</code>.\n"
            "Create e.g. <code>data/CCNA-200-301/questions/</code>.",
        )
        return
    rows = [[(f"{_cert_icon(c)} {esc(c)}", f"cert:{c}")] for c in certs]
    rows.append([("📊 Exam History", "history")])
    await _edit(update, "🎯 <b>Quiz-App</b>\n\nChoose a certification:", _kb(rows))


async def render_mode_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = _ud(context)
    ud["state"] = "mode"
    cert  = ud["cert"]
    total = len(ud.get("questions", []))
    rows = [
        [("📘 Study  — explanations + ordered selection", "mode:study")],
        [("🎯 Exam   — random, timed, results saved",     "mode:exam")],
        _nav("🔙 Change Cert"),
    ]
    await _edit(
        update,
        f"<b>{esc(cert)}</b>  —  {total} question(s) loaded\n\n🎮 Choose mode:",
        _kb(rows),
    )


async def render_topic_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = _ud(context)
    ud["state"] = "topic"
    questions  = ud["questions"]
    topics     = available_topics(questions)
    mode       = ud.get("mode", Mode.STUDY)
    default_sel = set(topics) if mode == Mode.EXAM else set()
    sel: set   = ud.setdefault("topics_selected", default_sel)
    t_ranges   = build_topic_ranges(questions)

    rows = []
    for t in topics:
        lo, hi, cnt = t_ranges[t]
        icon  = "🔘" if (mode == Mode.STUDY and t in sel) else \
                "⚪" if mode == Mode.STUDY else \
                "✅" if t in sel else "⬜"
        label = f"{icon} {esc(t)}  #{lo}–#{hi}  ({cnt})"
        rows.append([(label, f"topic:{t}")])

    if mode == Mode.EXAM:
        rows.append([("✅ All", "topic_all"), ("❌ None", "topic_none")])
    rows.append([_nav()[0], ("▶ Next →", "topic_done")])
    prompt = "🧩 <b>Select Topics</b> — tap to toggle:" if mode == Mode.EXAM else \
             "📘 <b>Select One Topic</b> — choose a single topic for Study mode:"
    await _edit(update, prompt, _kb(rows))


async def render_count_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exam mode: choose how many random questions to answer."""
    ud = _ud(context)
    ud["state"] = "count"
    pool_size = len(ud.get("topic_pool", []))

    rows = []
    for n in EXAM_COUNTS:
        if n <= pool_size:
            rows.append([(f"🎯 {n} questions", f"count:{n}")])
        else:
            rows.append([(f"🎯 {n} questions  (max {pool_size})", f"count:{pool_size}")])
    rows.append([(f"📋 All available  ({pool_size})", f"count:{pool_size}")])
    rows.append(_nav())

    await _edit(
        update,
        f"🎯 <b>Exam Mode</b>\n"
        f"Pool: <b>{pool_size}</b> questions from selected topics.\n\n"
        f"How many questions do you want?",
        _kb(rows),
    )


async def render_study_select_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Study mode: choose all questions or a block of 10 inside the selected topic."""
    ud = _ud(context)
    ud["state"] = "study_select"
    questions  = ud["questions"]
    sel_topics = list(ud.get("topics_selected", set()))
    topic = sel_topics[0] if sel_topics else ""
    pool = [q for q in questions if q.topic == topic]

    rows = [[(f"📋 All questions ({len(pool)} q)", "study:ALL")]]
    for start in range(0, len(pool), 10):
        end = min(start + 10, len(pool))
        rows.append([(f"📘 Questions {start + 1}-{end}", f"study:block:{start}:{end}")])
    rows.append(_nav())

    lines = [
        f"📘 <b>Study Mode</b>  —  {esc(ud['cert'])}",
        "",
        f"Topic: <b>{esc(topic)}</b>",
        f"Total questions: <b>{len(pool)}</b>",
        "",
        "Choose the whole topic or one block of 10 questions:",
    ]

    await _edit(update, "\n".join(lines), _kb(rows))


# ── cert ──────────────────────────────────────────────────────────────────────

async def on_cert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    cert = update.callback_query.data.split(":", 1)[1]
    ud = _ud(context)
    ud["cert"] = cert
    try:
        ud["questions"] = load_questions(cert)
    except Exception as exc:
        await _edit(update, f"⚠️ Error loading questions:\n<code>{esc(exc)}</code>")
        return
    for key in ("mode", "topics_selected", "topic_pool", "filtered_questions"):
        ud.pop(key, None)
    await render_mode_screen(update, context)


# ── mode ──────────────────────────────────────────────────────────────────────

async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    ud["mode"] = Mode(update.callback_query.data.split(":", 1)[1])
    ud["topics_selected"] = set(available_topics(ud["questions"])) if ud["mode"] == Mode.EXAM else set()
    await render_topic_screen(update, context)


# ── topics ────────────────────────────────────────────────────────────────────

async def on_topic_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    data = update.callback_query.data
    ud   = _ud(context)
    mode = ud.get("mode", Mode.STUDY)
    sel: set   = ud.setdefault("topics_selected", set())
    all_topics = available_topics(ud["questions"])
    if data == "topic_all":
        ud["topics_selected"] = set(all_topics)
    elif data == "topic_none":
        ud["topics_selected"] = set()
    else:
        t = data[6:]
        if mode == Mode.STUDY:
            ud["topics_selected"] = set() if t in sel else {t}
        else:
            sel.discard(t) if t in sel else sel.add(t)
    await render_topic_screen(update, context)


async def on_topic_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if not ud.get("topics_selected"):
        await update.callback_query.answer("Select at least one topic!", show_alert=True)
        return
    if ud["mode"] == Mode.EXAM:
        ud["topic_pool"] = [q for q in ud["questions"] if q.topic in ud["topics_selected"]]
        await render_count_screen(update, context)
    else:
        if len(ud["topics_selected"]) != 1:
            await update.callback_query.answer("Study mode allows only one topic.", show_alert=True)
            return
        await render_study_select_screen(update, context)


# ── exam: count ───────────────────────────────────────────────────────────────

async def on_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    import random
    n  = int(update.callback_query.data.split(":", 1)[1])
    ud = _ud(context)
    pool = ud["topic_pool"]
    ud["filtered_questions"] = random.sample(pool, min(n, len(pool)))
    await _start_quiz(update, context)


# ── study: select range ───────────────────────────────────────────────────────

async def on_study_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    ud  = _ud(context)
    questions = ud["questions"]
    sel_topics = list(ud.get("topics_selected", set()))
    topic = sel_topics[0] if sel_topics else ""
    pool = [q for q in questions if q.topic == topic]

    if len(parts) == 2 and parts[1] == "ALL":
        ud["filtered_questions"] = pool
    elif len(parts) == 4 and parts[1] == "block":
        start = int(parts[2])
        end = int(parts[3])
        ud["filtered_questions"] = pool[start:end]
    else:
        ud["filtered_questions"] = pool

    await _start_quiz(update, context)


# ── quiz start ────────────────────────────────────────────────────────────────

async def _start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud   = _ud(context)
    pool = list(ud.get("filtered_questions") or ud["topic_pool"])
    if not pool:
        await _edit(update, "⚠️ No questions found for this selection.")
        return
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)
    ud["session"]    = QuizSession(mode=ud["mode"], questions=pool)
    ud["state"]      = "quiz"
    ud["quiz_start"] = time.time()
    await _send_question(update, context)


# ── question display ──────────────────────────────────────────────────────────

async def _send_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud      = _ud(context)
    session = _session(context)
    q       = session.current_question
    if q is None:
        await _show_result(update, context)
        return

    idx   = session.current_index
    total = len(session.questions)
    type_hint = {
        QuestionType.SINGLE_CHOICE:   "🎯 Choose ONE answer",
        QuestionType.MULTIPLE_CHOICE: "🧠 Choose ALL correct answers, then Submit",
        QuestionType.EQUIVALENCE:     "🧩 Tap a left item, then a right item to match",
    }[q.type]

    topo = f"\n<pre>{esc(render_topology(q.topology))}</pre>" if q.topology else ""

    # Exam mode shows elapsed time in header
    timer_txt = ""
    if ud["mode"] == Mode.EXAM:
        elapsed = int(time.time() - ud.get("quiz_start", time.time()))
        timer_txt = f"  ⏱ {_fmt_time(elapsed)}"

    options_text = ""
    if q.type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
        options_text = "\n\n" + "\n".join(
            f"<b>{k}.</b> {esc(v)}" for k, v in (q.options or {}).items()
        )

    text = (
        f"<b>Question {idx + 1}/{total}</b>{timer_txt}  |  "
        f"{esc(q.topic)}  <code>[{esc(q.id)}]</code>\n"
        f"{type_hint}\n"
        f"{topo}\n"
        f"<b>{esc(q.question)}</b>"
        f"{options_text}"
    )

    if q.type == QuestionType.MULTIPLE_CHOICE:
        ud["mc_selected"] = set()
    if q.type == QuestionType.EQUIVALENCE:
        ud["eq_matches"] = {}
        ud["eq_pending_left"] = None

    await _edit(update, text, _build_question_keyboard(q, ud))


def _build_question_keyboard(q: Question, ud: dict) -> InlineKeyboardMarkup:
    nav = _nav("⚠️ Exit Quiz")
    skip_row = [("▶ Next Question", "next")]

    if q.type == QuestionType.SINGLE_CHOICE:
        rows = [[(f"{k}", f"opt:{k}")] for k in (q.options or {})]
        rows.append(skip_row)
        rows.append(nav)
        return _kb(rows)

    if q.type == QuestionType.MULTIPLE_CHOICE:
        sel: set = ud.get("mc_selected", set())
        rows = [
            [(f"{'✅' if k in sel else '⬜'} {k}", f"mc:{k}")]
            for k in (q.options or {})
        ]
        rows.append([("✅ Submit Answer", "mc_submit")])
        rows.append(skip_row)
        rows.append(nav)
        return _kb(rows)

    if q.type == QuestionType.EQUIVALENCE:
        matches: dict       = ud.get("eq_matches", {})
        pending: str | None = ud.get("eq_pending_left")
        unmatched_left  = [k for k in (q.left_items or {}) if k not in matches]
        matched_right   = set(matches.values())
        rows = []
        for lk in unmatched_left:
            icon = "🔵" if lk == pending else "⬜"
            rows.append([(f"{icon} {lk}: {esc(q.left_items[lk])}", f"eq_left:{lk}")])
        if pending is not None:
            rows.append([("— match to →", "noop")])
            for rk, rv in (q.right_items or {}).items():
                if rk not in matched_right:
                    rows.append([(f"→ {rk}: {esc(rv)}", f"eq_right:{rk}")])
        if not unmatched_left:
            rows.append([("✅ Submit Matches", "eq_submit")])
        rows.append(skip_row)
        rows.append(nav)
        return _kb(rows)

    return _kb([skip_row, nav])


def _eq_matches_text(q: Question, matches: dict) -> str:
    if not matches:
        return ""
    lines = ["\n📋 <b>Matches so far:</b>"]
    for lk, rk in matches.items():
        lines.append(
            f"  {lk}: {esc(q.left_items.get(lk, lk))}  →  "
            f"{rk}: {esc(q.right_items.get(rk, rk))}"
        )
    return "\n".join(lines)


# ── answer handlers ───────────────────────────────────────────────────────────

async def on_single_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    await _process_answer(update, context, update.callback_query.data.split(":", 1)[1])


async def on_mc_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    key = update.callback_query.data.split(":", 1)[1]
    sel: set = ud.setdefault("mc_selected", set())
    sel.discard(key) if key in sel else sel.add(key)
    await update.callback_query.edit_message_reply_markup(
        _build_question_keyboard(_session(context).current_question, ud)
    )


async def on_mc_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    if not ud.get("mc_selected"):
        await update.callback_query.answer("Select at least one option!", show_alert=True)
        return
    await _process_answer(update, context, set(ud["mc_selected"]))


async def on_eq_left(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    lk = update.callback_query.data.split(":", 1)[1]
    ud["eq_pending_left"] = lk
    q = _session(context).current_question
    s = _session(context)
    text = (
        f"<b>Question {s.current_index + 1}/{len(s.questions)}</b>  |  {esc(q.topic)}\n"
        f"<b>{esc(q.question)}</b>"
        f"{_eq_matches_text(q, ud.get('eq_matches', {}))}\n\n"
        f"<b>Match:</b> {lk}: {esc(q.left_items.get(lk, lk))}  →  ?"
    )
    await _edit(update, text, _build_question_keyboard(q, ud))


async def on_eq_right(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    rk  = update.callback_query.data.split(":", 1)[1]
    lk  = ud.get("eq_pending_left")
    if not lk:
        return
    ud.setdefault("eq_matches", {})[lk] = rk
    ud["eq_pending_left"] = None
    q = _session(context).current_question
    s = _session(context)
    text = (
        f"<b>Question {s.current_index + 1}/{len(s.questions)}</b>  |  {esc(q.topic)}\n"
        f"<b>{esc(q.question)}</b>"
        f"{_eq_matches_text(q, ud.get('eq_matches', {}))}"
    )
    await _edit(update, text, _build_question_keyboard(q, ud))


async def on_eq_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    matches = dict(ud.get("eq_matches", {}))
    await _process_answer(update, context, matches)
    ud["eq_matches"] = {}
    ud["eq_pending_left"] = None


async def on_lab_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    q = _session(context).current_question
    if not q or q.type != QuestionType.SIMULATION_LAB:
        return
    state = _ensure_lab_state(ud, q)
    missing_required, missing_verify = _lab_missing_commands(q, state)
    if missing_required or missing_verify:
        missing = []
        if missing_required:
            missing.append("required: " + ", ".join(missing_required[:3]))
        if missing_verify:
            missing.append("verify: " + ", ".join(missing_verify[:3]))
        await update.callback_query.answer("Missing steps ? " + " | ".join(missing), show_alert=True)
        return
    await _process_answer(update, context, {"commands": list(state.get("normalized_history", []))})


async def on_lab_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    q = _session(context).current_question
    if not q or q.type != QuestionType.SIMULATION_LAB:
        return
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)
    await _send_question(update, context)


async def on_lab_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = _ud(context)
    if ud.get("state") != "quiz":
        return
    session = ud.get("session")
    q = session.current_question if session else None
    if not q or q.type != QuestionType.SIMULATION_LAB or not q.lab:
        return

    raw = (update.effective_message.text or "").strip()
    if not raw:
        return

    normalized = _normalize_lab_command(raw)
    state = _ensure_lab_state(ud, q)
    prompt_before = state.get("prompt", q.lab.initial_prompt)
    state.setdefault("raw_history", []).append(raw)
    state.setdefault("normalized_history", []).append(normalized)
    state.setdefault("normalized_set", set()).add(normalized)

    known = _lab_known_commands(q)
    response = q.lab.command_responses.get(normalized)
    if response is None:
        if normalized in known:
            response = "OK"
        else:
            response = "% Unsupported command for this lab scenario."

    state["prompt"] = _next_lab_prompt(prompt_before, normalized, q.lab.initial_prompt)
    missing_required, missing_verify = _lab_missing_commands(q, state)
    completion_hint = ""
    if not missing_required and not missing_verify:
        completion_hint = "\n\n? Required lab steps complete. Tap Submit Lab when ready."

    lines = [f"<code>{esc(prompt_before)} {raw}</code>"]
    if response:
        lines.append(f"<pre>{esc(response)}</pre>")
    lines.append(f"<code>{esc(state['prompt'])}</code>{completion_hint}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")




async def _process_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, answer) -> None:
    ud      = _ud(context)
    session = _session(context)
    q       = session.current_question
    correct = submit_answer(session, answer)
    ud["state"] = "answered"

    icon = "✅" if correct else "❌"
    if correct:
        feedback = f"{icon} <b>Correct!</b>"
    elif q.type == QuestionType.SINGLE_CHOICE:
        feedback = f"{icon} <b>Wrong.</b>  Correct: <b>{esc(q.correct_answer[0])}</b>"
    elif q.type == QuestionType.MULTIPLE_CHOICE:
        feedback = f"{icon} <b>Wrong.</b>  Correct: <b>{esc(', '.join(sorted(q.correct_answer or [])))}</b>"
    else:
        lines = [
            f"  {lk} → {rk}  "
            f"({esc(q.left_items.get(lk,''))} = {esc(q.right_items.get(rk,''))})"
            for lk, rk in (q.correct_matches or {}).items()
        ]
        feedback = f"{icon} <b>Wrong.</b>  Correct:\n" + "\n".join(lines)

    # Study mode: show explanation immediately
    extra = ""
    if session.mode == Mode.STUDY:
        if q.explanation:
            extra += f"\n\n📘 <i>{esc(q.explanation)}</i>"
        if q.exam_tip:
            extra += f"\n\n💡 <b>Tip:</b> {esc(q.exam_tip)}"

    done  = session.current_index
    total = len(session.questions)
    progress = f"\n\n<code>{done}/{total} answered</code>"

    markup = (
        _kb([[("🏁 See Results", "results")]])
        if session.is_finished
        else _kb([[("▶ Next Question", "next")]])
    )
    await _edit(update, feedback + extra + progress, markup)


# ── next / results ────────────────────────────────────────────────────────────

async def on_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    s = _session(context)
    if ud.get("state") == "quiz":
        s.skip_current()
    ud["state"] = "quiz"
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)
    await (_show_result if s.is_finished else _send_question)(update, context)


async def on_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    if not _session(context).is_finished:
        await update.callback_query.answer("Answer all questions before viewing results.", show_alert=True)
        return
    await _show_result(update, context)


async def _show_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud      = _ud(context)
    session = _session(context)
    if not session.is_finished:
        return
    cert    = ud.get("cert", "")
    mode    = ud.get("mode", Mode.STUDY)
    username = str(update.effective_user.id)

    s      = summary(session)
    pct    = s["score_pct"]
    passed = pct >= PASS_THRESHOLD

    # Timer (exam only)
    elapsed_txt = ""
    if mode == Mode.EXAM:
        elapsed = int(time.time() - ud.get("quiz_start", time.time()))
        elapsed_txt = f"\n⏱ Time: <b>{_fmt_time(elapsed)}</b>"
        save_session(cert, username, {**s, "elapsed_seconds": elapsed})

    verdict = "🏆 PASSED" if passed else "❌ FAILED"
    mode_label = "🎯 Exam" if mode == Mode.EXAM else "📘 Study"

    lines = [
        f"<b>{verdict}  —  {esc(cert)}</b>",
        f"{mode_label}",
        "",
        f"Score: <b>{s['correct']}/{s['total']}</b>  ({pct}%)",
        f"Threshold: {PASS_THRESHOLD}%",
        elapsed_txt,
    ]

    if s["wrong_ids"]:
        lines.append("\n<b>Review these questions:</b>")
        q_map = {q.id: q for q in session.questions}
        for qid in s["wrong_ids"]:
            q = q_map.get(qid)
            snippet = esc(q.question[:60]) + "…" if q else esc(qid)
            lines.append(f"  • <code>[{esc(qid)}]</code> {snippet}")

    rows = [[("🔄 Retry", "retry")]]
    if mode == Mode.EXAM:
        rows.append([("📊 Exam History", "history")])
    rows.append([("🏠 Home", "nav_home")])

    await _edit(update, "\n".join(lines), _kb(rows))
    ud["state"] = "done"


async def on_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await _start_quiz(update, context)


# ── navigation ────────────────────────────────────────────────────────────────

_BACK_MAP = {
    "mode":         render_cert_screen,
    "topic":        render_mode_screen,
    "count":        render_topic_screen,
    "study_select": render_topic_screen,
    "done":         render_cert_screen,
}


async def on_nav_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud    = _ud(context)
    state = ud.get("state", "cert")
    if state in ("quiz", "answered"):
        await _show_exit_warning(update, context, target="back")
        return
    renderer = _BACK_MAP.get(state, render_cert_screen)
    await renderer(update, context)


async def on_nav_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    if ud.get("state") in ("quiz", "answered"):
        await _show_exit_warning(update, context, target="home")
        return
    context.user_data.clear()
    await render_cert_screen(update, context)


async def _show_exit_warning(
    update: Update, context: ContextTypes.DEFAULT_TYPE, target: str
) -> None:
    ud      = _ud(context)
    session = ud.get("session")
    answered = session.current_index if session else 0
    total    = len(session.questions) if session else 0
    ud["pending_nav"] = target
    await _edit(
        update,
        f"⚠️ <b>Exit quiz?</b>\n\n"
        f"You have answered <b>{answered}/{total}</b> questions.\n"
        f"<b>Progress will be lost.</b>",
        _kb([[("✅ Yes, exit", "nav_confirm"), ("❌ No, continue", "nav_cancel")]]),
    )


async def on_nav_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud     = _ud(context)
    target = ud.pop("pending_nav", "home")
    # preserve cert + questions for re-navigation
    cert      = ud.get("cert")
    questions = ud.get("questions")
    mode      = ud.get("mode")
    context.user_data.clear()
    if cert:
        ud["cert"]      = cert
        ud["questions"] = questions
        ud["mode"]      = mode
    if target == "back":
        await render_topic_screen(update, context)
    else:
        context.user_data.clear()
        await render_cert_screen(update, context)


async def on_nav_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud = _ud(context)
    ud.pop("pending_nav", None)
    ud["state"] = "quiz"
    await _send_question(update, context)


# ── history (exam only) ───────────────────────────────────────────────────────

async def on_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    ud       = _ud(context)
    cert     = ud.get("cert")
    username = str(update.effective_user.id)

    if not cert:
        await _edit(update, "Complete an Exam first to see history.", _kb([_nav()]))
        return

    history = load_history(cert, username)[-15:]
    if not history:
        lines = [f"📊 <b>No exam history for {esc(cert)} yet.</b>"]
    else:
        lines = [f"📊 <b>Exam History — {esc(cert)}</b>\n"]
        for h in reversed(history):
            badge  = "✅" if h["score_pct"] >= PASS_THRESHOLD else "❌"
            timing = f"  ⏱ {_fmt_time(h['elapsed_seconds'])}" if "elapsed_seconds" in h else ""
            lines.append(
                f"  {badge} {h['date']}  {h['correct']}/{h['total']} ({h['score_pct']}%){timing}"
            )

    await _edit(update, "\n".join(lines), _kb([_nav()]))


# ── misc ──────────────────────────────────────────────────────────────────────

async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await render_cert_screen(update, context)


# ── app builder ───────────────────────────────────────────────────────────────

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_lab_text))

    # Flow
    app.add_handler(CallbackQueryHandler(on_cert,         pattern=r"^cert:"))
    app.add_handler(CallbackQueryHandler(on_mode,         pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_topic_done,   pattern=r"^topic_done$"))
    app.add_handler(CallbackQueryHandler(on_topic_toggle, pattern=r"^topic"))
    app.add_handler(CallbackQueryHandler(on_count,        pattern=r"^count:"))
    app.add_handler(CallbackQueryHandler(on_study_select, pattern=r"^study:"))

    # Quiz
    app.add_handler(CallbackQueryHandler(on_single_answer, pattern=r"^opt:"))
    app.add_handler(CallbackQueryHandler(on_mc_toggle,     pattern=r"^mc:[^_]"))
    app.add_handler(CallbackQueryHandler(on_mc_submit,     pattern=r"^mc_submit$"))
    app.add_handler(CallbackQueryHandler(on_eq_left,       pattern=r"^eq_left:"))
    app.add_handler(CallbackQueryHandler(on_eq_right,      pattern=r"^eq_right:"))
    app.add_handler(CallbackQueryHandler(on_eq_submit,     pattern=r"^eq_submit$"))
    app.add_handler(CallbackQueryHandler(on_lab_submit,    pattern=r"^lab_submit$"))
    app.add_handler(CallbackQueryHandler(on_lab_reset,     pattern=r"^lab_reset$"))
    app.add_handler(CallbackQueryHandler(on_next,          pattern=r"^next$"))
    app.add_handler(CallbackQueryHandler(on_results,       pattern=r"^results$"))
    app.add_handler(CallbackQueryHandler(on_retry,         pattern=r"^retry$"))

    # Navigation
    app.add_handler(CallbackQueryHandler(on_nav_back,    pattern=r"^nav_back$"))
    app.add_handler(CallbackQueryHandler(on_nav_home,    pattern=r"^nav_home$"))
    app.add_handler(CallbackQueryHandler(on_nav_confirm, pattern=r"^nav_confirm$"))
    app.add_handler(CallbackQueryHandler(on_nav_cancel,  pattern=r"^nav_cancel$"))

    # History / misc
    app.add_handler(CallbackQueryHandler(on_history, pattern=r"^history$"))
    app.add_handler(CallbackQueryHandler(on_noop,    pattern=r"^noop$"))

    return app
