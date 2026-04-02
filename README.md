# Quiz-App — Certification Quiz Bot

A Telegram bot for studying certification exams (CCNA 200-301, CompTIA, AWS, and more).
Questions are stored in YAML files and grouped by certification and topic.

---

## Features

- ✅ **Single-choice**, 🧠 **multiple-choice**, and 🧩 **matching** questions
- 🖧 **Topology support** (ASCII diagrams)
- 📘 **Study mode** — explanation + exam tip after each answer, range or topic-based selection
- 🎯 **Exam mode** — shuffled random questions, countdown timer, score at the end
- 📊 **Progress history** saved per user per certification (Exam mode only)
- 🏆 **Pass/Fail** threshold at 80%
- 🔒 **100% button-driven** — no text input required in Telegram

---

## Project Structure

```
Quiz-App/
├── bot_telegram.py             # Entry point + token wizard
├── .env                        # TELEGRAM_BOT_TOKEN (not committed)
├── .env.example
├── requirements.txt
├── logs/                       # Auto-created, daily rotating log files
├── data/
│   └── CCNA-200-301/
│       ├── questions/          # YAML question files (one file per topic)
│       │   ├── routing_example.yaml
│       │   ├── security_example.yaml
│       │   └── switching_example.yaml
│       └── users/              # Per-user exam history (JSON, auto-created)
└── src/
    ├── models.py               # Dataclasses: Question, QuizSession, etc.
    ├── loader.py               # YAML loader with sequential ID mapping
    ├── quiz_engine.py          # Session builder and answer logic
    ├── progress.py             # Exam history persistence
    ├── topology_renderer.py    # ASCII topology display
    └── telegram_bot.py         # All bot handlers, keyboards, and screens
```

---

## Installation

```bash
pip install -r requirements.txt
```

---

## Running

```bash
# From the project root (C:\Automation\Quiz-App)
python bot_telegram.py
```

On first run, if no `.env` exists, the bot will prompt for the Telegram token.

---

## Adding a New Certification

Create a new folder under `data/`:

```
data/CompTIA-Network+/questions/
data/AWS-SAA-C03/questions/
```

The bot auto-discovers all certification folders at startup.

---

## YAML Question Format

### single_choice

```yaml
- id: SW-001
  topic: Switching
  subtopic: VLANs
  type: single_choice
  tags: [vlan, switching]
  question: "Which command assigns a port to VLAN 10?"
  options:
    A: "switchport mode trunk"
    B: "switchport access vlan 10"
    C: "vlan 10 access"
  correct_answer: B
  explanation: "..."
  exam_tip: "..."
  source: original
```

### multiple_choice

```yaml
- id: SW-004
  type: multiple_choice
  correct_answer: [B, C]    # list of all correct options
```

### equivalence_buttons (matching)

```yaml
- id: SW-003
  type: equivalence_buttons
  left_items:
    A: STP
    B: OSPF
  right_items:
    "1": Prevents Layer 2 loops
    "2": Dynamic routing
  correct_matches:
    A: "1"
    B: "2"
```

### With topology (ASCII preferred)

```yaml
  topology:
    type: ascii
    ascii_diagram: |
        PC-A
         |
        SW1
         |
        R1
```

---

## Sequential Question IDs

Questions are numbered globally (1, 2, 3…) in file-alphabetical order.
The bot displays each topic's range, e.g.:

```
Routing      #1–4
Security     #5–6
Switching    #7–10
```

Use these numbers when selecting ranges in Study mode.

---

## Modes

| | Study | Exam |
|---|---|---|
| Topic selection | Toggle topics | Toggle topics |
| Question selection | By topic range (buttons) | Random N (10/50/70/100/120) |
| Order | In file order | Shuffled |
| Explanation shown | ✅ After each answer | ❌ Only at end |
| Timer | ❌ | ✅ |
| Progress saved | ❌ | ✅ (≥80% = pass) |


## Linux deployment

Use `deploy/install_linux.sh` on the server to clone/update the app into `/home/beneves/quiz-app`, create `.venv`, install requirements, and register both `systemd` services.

Services:
- `deploy/systemd/quiz-app-telegram.service`
- `deploy/systemd/quiz-app-discord.service`

The server must have a valid `/home/beneves/quiz-app/.env` with `TELEGRAM_BOT_TOKEN` and `DISCORD_TOKEN`.
