"""
Discord bot — Quiz-App.

Flow mirrors Telegram bot:
  !quiz → cert → mode → topic → [count | study_select] → quiz → results

Commands:
  !quiz     — start or restart the quiz
  !history  — show last 15 exam results for current cert
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import discord
from discord.ext import commands

from src.loader import available_certs, available_topics, build_topic_ranges, load_questions
from src.models import Mode, Question, QuestionType, QuizSession
from src.progress import load_history, save_session
from src.quiz_engine import submit_answer, summary
from src.topology_renderer import render as render_topology

log = logging.getLogger(__name__)

PASS_THRESHOLD = 80
EXAM_COUNTS    = [10, 50, 70, 100, 120]

# ── per-user state ────────────────────────────────────────────────────────────

_sessions: dict[int, dict] = {}


def _ud(uid: int) -> dict:
    return _sessions.setdefault(uid, {})


def _session(uid: int) -> QuizSession:
    return _ud(uid)["session"]


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_time(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s"


def _normalize_lab_command(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _question_image_path(cert: str, q: Question) -> str | None:
    rel = ""
    if q.type == QuestionType.SIMULATION_LAB and q.lab and q.lab.image:
        rel = q.lab.image.strip()
    elif q.source:
        rel = str(q.source).split(",")[0].strip()
    if not rel:
        return None
    if os.path.isabs(rel):
        return rel
    return os.path.join(ROOT, "data", cert, "images", rel)


async def _maybe_send_lab_image(target, uid: int, q: Question) -> None:
    if q.type != QuestionType.SIMULATION_LAB:
        return
    ud = _ud(uid)
    if ud.get("lab_image_for") == q.id:
        return
    cert = ud.get("cert", "")
    image_path = _question_image_path(cert, q)
    if not image_path or not os.path.isfile(image_path):
        return
    channel = getattr(target, "channel", None)
    if channel is None and hasattr(target, "message"):
        channel = getattr(target.message, "channel", None)
    if channel is None:
        return
    await channel.send(content=f"??? Exhibit for `[{q.id}]`", file=discord.File(image_path))
    ud["lab_image_for"] = q.id


def _ensure_lab_state(ud: dict, q: Question) -> dict:
    state = ud.get("lab_state")
    if state and state.get("question_id") == q.id:
        return state
    prompt = q.lab.initial_prompt if q.lab else "Device#"
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
    return set(q.lab.required_commands) | set(q.lab.verification_commands) | set(q.lab.command_responses.keys())


def _lab_missing_commands(q: Question, state: dict) -> tuple[list[str], list[str]]:
    issued = state.get("normalized_set", set())
    required = list(q.lab.required_commands if q.lab else [])
    verify = list(q.lab.verification_commands if q.lab else [])
    missing_required = [cmd for cmd in required if cmd not in issued]
    missing_verify = [cmd for cmd in verify if cmd not in issued]
    return missing_required, missing_verify


def _next_lab_prompt(current_prompt: str, normalized: str, default_prompt: str) -> str:
    if not normalized:
        return current_prompt
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
    return current_prompt


def _cert_icon(cert: str) -> str:
    for key, icon in {"CCNA": "🌐", "CCNP": "🌐", "CompTIA": "💻",
                      "AWS": "☁️", "Azure": "☁️", "GCP": "☁️"}.items():
        if key in cert:
            return icon
    return "📘"


def _trunc(s: str, n: int = 78) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _make_view(
    rows: list[list[tuple[str, any, discord.ButtonStyle]]],
    timeout: int = 600,
) -> discord.ui.View:
    """Build a discord.ui.View from rows of (label, callback, style) tuples."""
    view = discord.ui.View(timeout=timeout)
    for row_idx, row_items in enumerate(rows[:5]):   # max 5 rows
        for label, callback, style in row_items[:5]: # max 5 per row
            btn = discord.ui.Button(label=_trunc(label), style=style, row=row_idx)
            btn.callback = callback
            view.add_item(btn)
    return view


async def _edit(target, text: str, view: discord.ui.View | None = None) -> None:
    """Send a new message (Context) or edit the existing one (Interaction)."""
    if isinstance(target, commands.Context):
        await target.send(content=text, view=view)
    else:
        await target.response.edit_message(content=text, view=view)


def _nav_row(uid: int, back_label: str = "🔙 Back") -> list[tuple]:
    async def _back(inter):
        await on_nav_back(inter, uid)

    async def _home(inter):
        await on_nav_home(inter, uid)

    return [
        (back_label, _back, discord.ButtonStyle.secondary),
        ("🏠 Home", _home, discord.ButtonStyle.secondary),
    ]


# ── screen renderers ──────────────────────────────────────────────────────────

async def render_cert_screen(target, uid: int) -> None:
    ud = _ud(uid)
    ud["state"] = "cert"
    certs = available_certs()
    if not certs:
        await _edit(target, "⚠️ No certification folders found in `data/`.\n"
                            "Create e.g. `data/CCNA-200-301/questions/`.")
        return

    rows = []
    for cert in certs:
        async def cert_cb(inter, c=cert):
            await on_cert(inter, uid, c)
        rows.append([(f"{_cert_icon(cert)} {cert}", cert_cb, discord.ButtonStyle.primary)])

    async def hist_cb(inter):
        await on_history_btn(inter, uid)

    rows.append([("📊 Exam History", hist_cb, discord.ButtonStyle.secondary)])
    await _edit(target, "🎯 **Quiz-App**\n\nChoose a certification:", _make_view(rows))


async def render_mode_screen(target, uid: int) -> None:
    ud = _ud(uid)
    ud["state"] = "mode"
    cert  = ud["cert"]
    total = len(ud.get("questions", []))

    async def study_cb(inter):
        await on_mode(inter, uid, Mode.STUDY)

    async def exam_cb(inter):
        await on_mode(inter, uid, Mode.EXAM)

    rows = [
        [("📘 Study — explanations + ordered", study_cb, discord.ButtonStyle.primary)],
        [("🎯 Exam — random, timed, results saved", exam_cb, discord.ButtonStyle.primary)],
        _nav_row(uid, "🔙 Change Cert"),
    ]
    await _edit(
        target,
        f"**{cert}**  —  {total} question(s) loaded\n\n🎮 Choose mode:",
        _make_view(rows),
    )


async def render_topic_screen(target, uid: int) -> None:
    ud        = _ud(uid)
    ud["state"] = "topic"
    questions = ud["questions"]
    topics    = available_topics(questions)
    mode      = ud.get("mode", Mode.STUDY)
    default_sel = set(topics) if mode == Mode.EXAM else set()
    sel: set  = ud.setdefault("topics_selected", default_sel)
    t_ranges  = build_topic_ranges(questions)

    rows = []
    for t in topics:
        lo, hi, cnt = t_ranges[t]
        icon = "🔘" if (mode == Mode.STUDY and t in sel) else \
               "⚪" if mode == Mode.STUDY else \
               "✅" if t in sel else "⬜"

        async def topic_cb(inter, topic=t):
            await on_topic_toggle(inter, uid, topic)

        rows.append([(f"{icon} {t}  #{lo}–#{hi}  ({cnt})", topic_cb, discord.ButtonStyle.secondary)])

    async def all_cb(inter):
        await on_topic_all(inter, uid)

    async def none_cb(inter):
        await on_topic_none(inter, uid)

    async def done_cb(inter):
        await on_topic_done(inter, uid)

    if mode == Mode.EXAM:
        rows.append([
            ("✅ All",  all_cb,  discord.ButtonStyle.secondary),
            ("❌ None", none_cb, discord.ButtonStyle.secondary),
        ])
    rows.append([
        *_nav_row(uid),
        ("▶ Next →", done_cb, discord.ButtonStyle.primary),
    ])
    text = "🧩 **Select Topics** — tap to toggle:" if mode == Mode.EXAM else \
           "📘 **Select One Topic** — choose a single topic for Study mode:"
    await _edit(target, text, _make_view(rows))


async def render_count_screen(target, uid: int) -> None:
    ud        = _ud(uid)
    ud["state"] = "count"
    pool_size = len(ud.get("topic_pool", []))

    rows = []
    for n in EXAM_COUNTS:
        actual = min(n, pool_size)
        label  = f"🎯 {n} questions" if n <= pool_size else f"🎯 {n} questions (max {pool_size})"

        async def count_cb(inter, count=actual):
            await on_count(inter, uid, count)

        rows.append([(label, count_cb, discord.ButtonStyle.primary)])

    async def all_cb(inter):
        await on_count(inter, uid, pool_size)

    rows.append([(f"📋 All available ({pool_size})", all_cb, discord.ButtonStyle.secondary)])
    rows.append(_nav_row(uid))
    await _edit(
        target,
        f"🎯 **Exam Mode**\nPool: **{pool_size}** questions from selected topics.\n\nHow many questions?",
        _make_view(rows),
    )


async def render_study_select_screen(target, uid: int) -> None:
    ud         = _ud(uid)
    ud["state"] = "study_select"
    questions  = ud["questions"]
    sel_topics = list(ud.get("topics_selected", set()))
    topic = sel_topics[0] if sel_topics else ""
    pool = [q for q in questions if q.topic == topic]

    rows = []
    async def all_cb(inter):
        await on_study_select(inter, uid, "ALL")
    rows.append([(f"📋 All questions ({len(pool)}q)", all_cb, discord.ButtonStyle.secondary)])

    for start in range(0, len(pool), 10):
        end = min(start + 10, len(pool))
        async def block_cb(inter, s=start, e=end):
            await on_study_select(inter, uid, f"BLOCK:{s}:{e}")
        rows.append([(f"📘 Questions {start + 1}-{end}", block_cb, discord.ButtonStyle.primary)])

    rows.append(_nav_row(uid))
    await _edit(
        target,
        f"📘 **Study Mode**  —  {ud['cert']}\n\nTopic: **{topic}**\nTotal questions: **{len(pool)}**\n\nChoose the whole topic or one block of 10 questions:",
        _make_view(rows),
    )


# ── flow handlers ─────────────────────────────────────────────────────────────

async def on_cert(inter: discord.Interaction, uid: int, cert: str) -> None:
    ud = _ud(uid)
    ud["cert"] = cert
    try:
        ud["questions"] = load_questions(cert)
    except Exception as exc:
        await inter.response.edit_message(
            content=f"⚠️ Error loading questions:\n```{exc}```", view=None
        )
        return
    for key in ("mode", "topics_selected", "topic_pool", "filtered_questions"):
        ud.pop(key, None)
    await render_mode_screen(inter, uid)


async def on_mode(inter: discord.Interaction, uid: int, mode: Mode) -> None:
    ud = _ud(uid)
    ud["mode"] = mode
    ud["topics_selected"] = set(available_topics(ud["questions"])) if mode == Mode.EXAM else set()
    await render_topic_screen(inter, uid)


async def on_topic_toggle(inter: discord.Interaction, uid: int, topic: str) -> None:
    ud  = _ud(uid)
    mode = ud.get("mode", Mode.STUDY)
    sel: set = ud.setdefault("topics_selected", set())
    if mode == Mode.STUDY:
        ud["topics_selected"] = set() if topic in sel else {topic}
    else:
        sel.discard(topic) if topic in sel else sel.add(topic)
    await render_topic_screen(inter, uid)


async def on_topic_all(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    ud["topics_selected"] = set(available_topics(ud["questions"]))
    await render_topic_screen(inter, uid)


async def on_topic_none(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    ud["topics_selected"] = set()
    await render_topic_screen(inter, uid)


async def on_topic_done(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    if not ud.get("topics_selected"):
        await inter.response.send_message("⚠️ Select at least one topic!", ephemeral=True)
        return
    if ud["mode"] == Mode.EXAM:
        ud["topic_pool"] = [q for q in ud["questions"] if q.topic in ud["topics_selected"]]
        await render_count_screen(inter, uid)
    else:
        if len(ud["topics_selected"]) != 1:
            await inter.response.send_message("⚠️ Study mode allows only one topic.", ephemeral=True)
            return
        await render_study_select_screen(inter, uid)


async def on_count(inter: discord.Interaction, uid: int, n: int) -> None:
    ud   = _ud(uid)
    pool = ud["topic_pool"]
    ud["filtered_questions"] = random.sample(pool, min(n, len(pool)))
    await _start_quiz(inter, uid)


async def on_study_select(inter: discord.Interaction, uid: int, val: str) -> None:
    ud        = _ud(uid)
    questions = ud["questions"]
    sel_topics = list(ud.get("topics_selected", set()))
    topic = sel_topics[0] if sel_topics else ""
    pool = [q for q in questions if q.topic == topic]
    if val == "ALL":
        ud["filtered_questions"] = pool
    elif val.startswith("BLOCK:"):
        _, start, end = val.split(":")
        ud["filtered_questions"] = pool[int(start):int(end)]
    else:
        ud["filtered_questions"] = pool
    await _start_quiz(inter, uid)


# ── quiz ──────────────────────────────────────────────────────────────────────

async def _start_quiz(target, uid: int) -> None:
    ud   = _ud(uid)
    pool = list(ud.get("filtered_questions") or ud["topic_pool"])
    if not pool:
        await _edit(target, "⚠️ No questions found for this selection.")
        return
    ud["session"]    = QuizSession(mode=ud["mode"], questions=pool)
    ud["state"]      = "quiz"
    ud["quiz_start"] = time.time()
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)
    await _send_question(target, uid)


async def _send_question(target, uid: int) -> None:
    ud      = _ud(uid)
    session = _session(uid)
    q       = session.current_question
    if q is None:
        await _show_result(target, uid)
        return

    idx   = session.current_index
    total = len(session.questions)
    type_hint = {
        QuestionType.SINGLE_CHOICE:   "Choose ONE answer",
        QuestionType.MULTIPLE_CHOICE: "Choose ALL correct answers, then Submit",
        QuestionType.EQUIVALENCE:     "Tap a left item, then a right item to match",
        QuestionType.DRAG_DROP:       "Tap a left item, then a right item to match",
        QuestionType.SIMULATION_LAB:  "Type commands in chat, then Submit Lab",
    }[q.type]

    timer_txt = ""
    if ud["mode"] == Mode.EXAM:
        elapsed = int(time.time() - ud.get("quiz_start", time.time()))
        timer_txt = f"  |  {_fmt_time(elapsed)}"

    topo = f"\n```\n{render_topology(q.topology)}\n```" if q.topology else ""
    exhibit = f"\n```text\n{q.exhibit}\n```" if q.exhibit else ""

    options_text = ""
    if q.type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
        options_text = "\n\n" + "\n".join(
            f"**{k}.** {v}" for k, v in (q.options or {}).items()
        )

    if q.type == QuestionType.MULTIPLE_CHOICE:
        ud["mc_selected"] = set()
    if q.type in (QuestionType.EQUIVALENCE, QuestionType.DRAG_DROP):
        ud["eq_matches"] = {}
        ud["eq_pending_left"] = None

    lab_text = ""
    if q.type == QuestionType.SIMULATION_LAB and q.lab:
        state = _ensure_lab_state(ud, q)
        objectives = "\n".join(f"- {item}" for item in q.lab.objectives)
        recent = "\n".join(state.get("raw_history", [])[-5:])
        recent_block = f"\n\n**Recent commands**\n```text\n{recent}\n```" if recent else ""
        objectives_block = f"\n\n**Objectives**\n{objectives}" if objectives else ""
        intro = f"\n\n{q.lab.intro}" if q.lab.intro else ""
        lab_text = (
            f"{intro}{objectives_block}{recent_block}"
            f"\n\n**Prompt**\n```text\n{state['prompt']}\n```"
        )

    text_out = (
        f"**Question {idx + 1}/{total}**{timer_txt}  |  {q.topic}  `[{q.id}]`\n"
        f"{type_hint}"
        f"{topo}"
        f"{exhibit}\n"
        f"**{q.question}**"
        f"{options_text}"
        f"{lab_text}"
    )

    await _edit(target, text_out, _build_question_view(q, ud, uid))
    if q.type == QuestionType.SIMULATION_LAB:
        await _maybe_send_lab_image(target, uid, q)


def _build_question_view(q: Question, ud: dict, uid: int) -> discord.ui.View:
    mode = ud.get("mode", Mode.STUDY)

    async def skip_cb(inter):
        await on_next_question(inter, uid)

    async def back_question_cb(inter):
        await on_prev_question(inter, uid)

    async def exit_cb(inter):
        await _show_exit_warning(inter, uid, "back")

    async def home_cb(inter):
        await _show_exit_warning(inter, uid, "home")

    nav = [
        ("⚠️ Exit Quiz", exit_cb, discord.ButtonStyle.danger),
        ("🏠 Home",      home_cb, discord.ButtonStyle.secondary),
    ]
    study_nav = [("◀ Back Question", back_question_cb, discord.ButtonStyle.secondary), ("▶ Next Question", skip_cb, discord.ButtonStyle.primary)] if mode == Mode.STUDY else None

    if q.type == QuestionType.SINGLE_CHOICE:
        rows = []
        for k in (q.options or {}):
            async def opt_cb(inter, key=k):
                await _process_answer(inter, uid, key)
            rows.append([(k, opt_cb, discord.ButtonStyle.primary)])
        if study_nav:
            rows.append(study_nav)
        rows.append(nav)
        return _make_view(rows)

    if q.type == QuestionType.MULTIPLE_CHOICE:
        sel: set = ud.get("mc_selected", set())
        rows = []
        for k in (q.options or {}):
            icon = "✅" if k in sel else "⬜"
            async def mc_cb(inter, key=k):
                await on_mc_toggle(inter, uid, key)
            rows.append([(f"{icon} {k}", mc_cb, discord.ButtonStyle.secondary)])
        async def submit_cb(inter):
            await on_mc_submit(inter, uid)
        rows.append([("✅ Submit Answer", submit_cb, discord.ButtonStyle.success)])
        if study_nav:
            rows.append(study_nav)
        rows.append(nav)
        return _make_view(rows)

    if q.type in (QuestionType.EQUIVALENCE, QuestionType.DRAG_DROP):
        matches: dict       = ud.get("eq_matches", {})
        pending: str | None = ud.get("eq_pending_left")
        unmatched_left      = [k for k in (q.left_items or {}) if k not in matches]
        matched_right       = set(matches.values())

        rows = []
        for lk in unmatched_left:
            icon = "🔵" if lk == pending else "⬜"
            async def eq_left_cb(inter, key=lk):
                await on_eq_left(inter, uid, key)
            rows.append([(f"{icon} {lk}: {q.left_items[lk]}", eq_left_cb, discord.ButtonStyle.secondary)])

        if pending is not None:
            for rk, rv in (q.right_items or {}).items():
                if rk not in matched_right:
                    async def eq_right_cb(inter, key=rk):
                        await on_eq_right(inter, uid, key)
                    rows.append([(f"→ {rk}: {rv}", eq_right_cb, discord.ButtonStyle.primary)])

        if not unmatched_left:
            async def eq_submit_cb(inter):
                await on_eq_submit(inter, uid)
            rows.append([("✅ Submit Matches", eq_submit_cb, discord.ButtonStyle.success)])

        if study_nav:
            rows.append(study_nav)
        rows.append(nav)
        return _make_view(rows)

    if q.type == QuestionType.SIMULATION_LAB:
        async def submit_lab_cb(inter):
            await on_lab_submit(inter, uid)
        async def reset_lab_cb(inter):
            await on_lab_reset(inter, uid)
        rows = [
            [("Submit Lab", submit_lab_cb, discord.ButtonStyle.success)],
            [("Reset Lab", reset_lab_cb, discord.ButtonStyle.secondary)],
            *([study_nav] if study_nav else []),
            nav,
        ]
        return _make_view(rows)

    rows = []
    if study_nav:
        rows.append(study_nav)
    rows.append(nav)
    return _make_view(rows)


# ── answer handlers ───────────────────────────────────────────────────────────

async def on_mc_toggle(inter: discord.Interaction, uid: int, key: str) -> None:
    ud = _ud(uid)
    if ud.get("state") != "quiz":
        return
    sel: set = ud.setdefault("mc_selected", set())
    sel.discard(key) if key in sel else sel.add(key)
    q    = _session(uid).current_question
    view = _build_question_view(q, ud, uid)
    await inter.response.edit_message(view=view)


async def on_mc_submit(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    if ud.get("state") != "quiz":
        return
    if not ud.get("mc_selected"):
        await inter.response.send_message("⚠️ Select at least one option!", ephemeral=True)
        return
    await _process_answer(inter, uid, set(ud["mc_selected"]))


async def on_eq_left(inter: discord.Interaction, uid: int, lk: str) -> None:
    ud = _ud(uid)
    if ud.get("state") != "quiz":
        return
    ud["eq_pending_left"] = lk
    q = _session(uid).current_question
    s = _session(uid)
    matches = ud.get("eq_matches", {})
    match_lines = "\n".join(
        f"  {l} → {r}  ({q.left_items.get(l,'')} = {q.right_items.get(r,'')})"
        for l, r in matches.items()
    )
    text = (
        f"**Question {s.current_index + 1}/{len(s.questions)}**  |  {q.topic}\n"
        f"**{q.question}**"
        + (f"\n\n📋 **Matches so far:**\n{match_lines}" if matches else "")
        + f"\n\n**Match:** {lk}: {q.left_items.get(lk, lk)}  →  ?"
    )
    view = _build_question_view(q, ud, uid)
    await inter.response.edit_message(content=text, view=view)


async def on_eq_right(inter: discord.Interaction, uid: int, rk: str) -> None:
    ud = _ud(uid)
    if ud.get("state") != "quiz":
        return
    lk = ud.get("eq_pending_left")
    if not lk:
        return
    ud.setdefault("eq_matches", {})[lk] = rk
    ud["eq_pending_left"] = None
    q = _session(uid).current_question
    s = _session(uid)
    matches = ud.get("eq_matches", {})
    match_lines = "\n".join(
        f"  {l} → {r}  ({q.left_items.get(l,'')} = {q.right_items.get(r,'')})"
        for l, r in matches.items()
    )
    text = (
        f"**Question {s.current_index + 1}/{len(s.questions)}**  |  {q.topic}\n"
        f"**{q.question}**"
        + (f"\n\n📋 **Matches so far:**\n{match_lines}" if matches else "")
    )
    view = _build_question_view(q, ud, uid)
    await inter.response.edit_message(content=text, view=view)


async def on_eq_submit(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    if ud.get("state") != "quiz":
        return
    matches = dict(ud.get("eq_matches", {}))
    await _process_answer(inter, uid, matches)
    ud["eq_matches"]      = {}
    ud["eq_pending_left"] = None


async def on_lab_submit(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    if ud.get("state") != "quiz":
        return
    q = _session(uid).current_question
    if not q or q.type != QuestionType.SIMULATION_LAB:
        return
    state = _ensure_lab_state(ud, q)
    missing_required, missing_verify = _lab_missing_commands(q, state)
    if missing_required or missing_verify:
        msg = []
        if missing_required:
            msg.append("Missing required commands:\n- " + "\n- ".join(missing_required))
        if missing_verify:
            msg.append("Missing verification commands:\n- " + "\n- ".join(missing_verify))
        await inter.response.send_message("\n\n".join(msg), ephemeral=True)
        return
    await _process_answer(inter, uid, {"commands": list(state.get("normalized_history", []))})


async def on_lab_reset(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)
    ud["state"] = "quiz"
    await _send_question(inter, uid)


async def on_lab_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    uid = message.author.id
    ud = _ud(uid)
    if ud.get("state") != "quiz" or "session" not in ud:
        return
    q = _session(uid).current_question
    if not q or q.type != QuestionType.SIMULATION_LAB or not q.lab:
        return
    raw = (message.content or "").strip()
    if not raw or raw.startswith("!"):
        return

    state = _ensure_lab_state(ud, q)
    prompt_before = state.get("prompt", q.lab.initial_prompt)
    normalized = _normalize_lab_command(raw)
    state.setdefault("normalized_history", []).append(normalized)
    state.setdefault("raw_history", []).append(raw)
    state.setdefault("normalized_set", set()).add(normalized)

    known = _lab_known_commands(q)
    response = q.lab.command_responses.get(normalized)
    if response is None:
        response = "OK" if normalized in known else "% Unsupported command for this lab scenario."

    state["prompt"] = _next_lab_prompt(prompt_before, normalized, q.lab.initial_prompt)
    missing_required, missing_verify = _lab_missing_commands(q, state)
    completion_hint = "\n\nRequired lab steps complete. Use the button above to submit." if not missing_required and not missing_verify else ""

    parts = [f"```text\n{prompt_before} {raw}\n```"]
    if response:
        parts.append(f"```text\n{response}\n```")
    parts.append(f"```text\n{state['prompt']}\n```{completion_hint}")
    await message.reply("\n".join(parts), mention_author=False)


async def on_next_question(target, uid: int) -> None:
    ud = _ud(uid)
    session = _session(uid)
    if ud.get("state") == "quiz":
        session.skip_current()
    ud["state"] = "quiz"
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)
    if session.is_finished:
        await _show_result(target, uid)
        return
    await _send_question(target, uid)


async def on_prev_question(target, uid: int) -> None:
    ud = _ud(uid)
    if ud.get("mode") != Mode.STUDY:
        return
    session = _session(uid)
    if ud.get("state") == "quiz":
        session.back_current()
    ud["state"] = "quiz"
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)
    await _send_question(target, uid)


async def _process_answer(target, uid: int, answer) -> None:
    ud      = _ud(uid)
    session = _session(uid)
    q       = session.current_question
    correct = submit_answer(session, answer)
    ud["state"] = "answered"
    ud.pop("lab_state", None)
    ud.pop("lab_image_for", None)

    icon = "✅" if correct else "❌"
    if correct:
        feedback = f"{icon} **Correct!**"
    elif q.type == QuestionType.SINGLE_CHOICE:
        feedback = f"{icon} **Wrong.**  Correct: **{q.correct_answer[0]}**"
    elif q.type == QuestionType.MULTIPLE_CHOICE:
        feedback = f"{icon} **Wrong.**  Correct: **{', '.join(sorted(q.correct_answer or []))}**"
    elif q.type == QuestionType.SIMULATION_LAB:
        feedback = f"{icon} **Lab not completed correctly.**"
    else:
        lines = [
            f"  {lk} → {rk}  ({q.left_items.get(lk,'')} = {q.right_items.get(rk,'')})"
            for lk, rk in (q.correct_matches or {}).items()
        ]
        feedback = f"{icon} **Wrong.**  Correct:\n" + "\n".join(lines)

    extra = ""
    if session.mode == Mode.STUDY:
        if q.explanation:
            extra += f"\n\n📘 *{q.explanation}*"
        if q.exam_tip:
            extra += f"\n\n💡 **Tip:** {q.exam_tip}"

    done  = session.current_index
    total = len(session.questions)
    progress = f"\n\n`{done}/{total} answered`"

    if session.is_finished:
        async def results_cb(inter):
            await _show_result(inter, uid)
        view = _make_view([[("🏁 See Results", results_cb, discord.ButtonStyle.success)]])
    else:
        async def next_cb(inter):
            await on_next_question(inter, uid)
        view = _make_view([[("▶ Next Question", next_cb, discord.ButtonStyle.primary)]])

    await _edit(target, feedback + extra + progress, view)


# ── results & history ─────────────────────────────────────────────────────────

async def _show_result(target, uid: int) -> None:
    ud      = _ud(uid)
    session = _session(uid)
    if not session.is_finished:
        return
    cert    = ud.get("cert", "")
    mode    = ud.get("mode", Mode.STUDY)
    username = str(uid)

    s      = summary(session)
    pct    = s["score_pct"]
    passed = pct >= PASS_THRESHOLD

    elapsed_txt = ""
    if mode == Mode.EXAM:
        elapsed = int(time.time() - ud.get("quiz_start", time.time()))
        elapsed_txt = f"\n⏱ Time: **{_fmt_time(elapsed)}**"
        save_session(cert, username, {**s, "elapsed_seconds": elapsed})

    verdict    = "🏆 PASSED" if passed else "❌ FAILED"
    mode_label = "🎯 Exam" if mode == Mode.EXAM else "📘 Study"

    lines = [
        f"**{verdict}  —  {cert}**",
        mode_label,
        "",
        f"Score: **{s['correct']}/{s['total']}**  ({pct}%)",
        f"Threshold: {PASS_THRESHOLD}%",
        elapsed_txt,
    ]

    if s["wrong_ids"]:
        lines.append("\n**Review these questions:**")
        q_map = {q.id: q for q in session.questions}
        for qid in s["wrong_ids"]:
            q       = q_map.get(qid)
            snippet = (q.question[:60] + "…") if q else qid
            lines.append(f"  • `[{qid}]` {snippet}")

    async def retry_cb(inter):
        await _start_quiz(inter, uid)

    async def home_cb(inter):
        _sessions.pop(uid, None)
        await render_cert_screen(inter, uid)

    rows = [[("🔄 Retry", retry_cb, discord.ButtonStyle.primary)]]
    if mode == Mode.EXAM:
        async def hist_cb(inter):
            await on_history_btn(inter, uid)
        rows.append([("📊 Exam History", hist_cb, discord.ButtonStyle.secondary)])
    rows.append([("🏠 Home", home_cb, discord.ButtonStyle.secondary)])

    await _edit(target, "\n".join(lines), _make_view(rows))
    ud["state"] = "done"


async def on_history_btn(target, uid: int) -> None:
    ud       = _ud(uid)
    cert     = ud.get("cert")
    username = str(uid)

    if not cert:
        await _edit(target, "⚠️ Complete an Exam first to see history.")
        return

    history = load_history(cert, username)[-15:]
    if not history:
        lines = [f"📊 **No exam history for {cert} yet.**"]
    else:
        lines = [f"📊 **Exam History — {cert}**\n"]
        for h in reversed(history):
            badge  = "✅" if h["score_pct"] >= PASS_THRESHOLD else "❌"
            timing = f"  ⏱ {_fmt_time(h['elapsed_seconds'])}" if "elapsed_seconds" in h else ""
            lines.append(
                f"  {badge} {h['date']}  {h['correct']}/{h['total']} ({h['score_pct']}%){timing}"
            )

    async def home_cb(inter):
        await render_cert_screen(inter, uid)

    view = _make_view([[("🏠 Home", home_cb, discord.ButtonStyle.secondary)]])
    await _edit(target, "\n".join(lines), view)


# ── navigation ────────────────────────────────────────────────────────────────

_BACK_MAP = {
    "mode":         render_cert_screen,
    "topic":        render_mode_screen,
    "count":        render_topic_screen,
    "study_select": render_topic_screen,
    "done":         render_cert_screen,
}


async def on_nav_back(target, uid: int) -> None:
    ud    = _ud(uid)
    state = ud.get("state", "cert")
    if state in ("quiz", "answered"):
        await _show_exit_warning(target, uid, "back")
        return
    renderer = _BACK_MAP.get(state, render_cert_screen)
    await renderer(target, uid)


async def on_nav_home(target, uid: int) -> None:
    ud = _ud(uid)
    if ud.get("state") in ("quiz", "answered"):
        await _show_exit_warning(target, uid, "home")
        return
    _sessions.pop(uid, None)
    await render_cert_screen(target, uid)


async def _show_exit_warning(target, uid: int, nav_target: str) -> None:
    ud       = _ud(uid)
    session  = ud.get("session")
    answered = session.current_index if session else 0
    total    = len(session.questions) if session else 0
    ud["pending_nav"] = nav_target

    async def confirm_cb(inter):
        await on_nav_confirm(inter, uid)

    async def cancel_cb(inter):
        await on_nav_cancel(inter, uid)

    view = _make_view([[
        ("✅ Yes, exit", confirm_cb, discord.ButtonStyle.danger),
        ("❌ No, continue", cancel_cb, discord.ButtonStyle.secondary),
    ]])
    await _edit(
        target,
        f"⚠️ **Exit quiz?**\n\n"
        f"You have answered **{answered}/{total}** questions.\n"
        f"**Progress will be lost.**",
        view,
    )


async def on_nav_confirm(inter: discord.Interaction, uid: int) -> None:
    ud        = _ud(uid)
    nav_target = ud.pop("pending_nav", "home")
    cert      = ud.get("cert")
    questions = ud.get("questions")
    mode      = ud.get("mode")
    _sessions.pop(uid, None)
    if cert:
        ud2 = _ud(uid)
        ud2["cert"]      = cert
        ud2["questions"] = questions
        ud2["mode"]      = mode
    if nav_target == "back":
        await render_topic_screen(inter, uid)
    else:
        _sessions.pop(uid, None)
        await render_cert_screen(inter, uid)


async def on_nav_cancel(inter: discord.Interaction, uid: int) -> None:
    ud = _ud(uid)
    ud.pop("pending_nav", None)
    ud["state"] = "quiz"
    await _send_question(inter, uid)


# ── bot builder ───────────────────────────────────────────────────────────────

def build_bot(token: str) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        log.info("Discord bot logged in as %s (id=%s)", bot.user, bot.user.id)
        print(f"\n🤖 Discord Quiz Bot running as {bot.user}. Press Ctrl+C to stop.\n")

    @bot.event
    async def on_message(message: discord.Message):
        await on_lab_message(message)
        await bot.process_commands(message)

    @bot.command(name="quiz")
    async def cmd_quiz(ctx: commands.Context):
        """Start (or restart) the certification quiz."""
        _sessions.pop(ctx.author.id, None)
        await render_cert_screen(ctx, ctx.author.id)

    @bot.command(name="history")
    async def cmd_history(ctx: commands.Context):
        """Show the last 15 exam results."""
        await on_history_btn(ctx, ctx.author.id)

    return bot
