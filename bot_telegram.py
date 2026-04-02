"""
Entry point for the Certification Quiz Telegram bot.

Token resolution order:
  1. Already set in environment (TELEGRAM_BOT_TOKEN)
  2. Exists in .env file
  3. Prompt the user to enter it → saved to .env
"""
from __future__ import annotations

import logging
import os
import sys

# ── token wizard ─────────────────────────────────────────────────────────────

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
ENV_KEY = "TELEGRAM_BOT_TOKEN"


def _read_env_file() -> dict[str, str]:
    """Parse .env into a dict without importing dotenv."""
    result: dict[str, str] = {}
    if not os.path.isfile(ENV_FILE):
        return result
    with open(ENV_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _write_token_to_env(token: str) -> None:
    """Write or update TELEGRAM_BOT_TOKEN in .env, preserving other keys."""
    env_data = _read_env_file()
    env_data[ENV_KEY] = token

    lines = []
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as fh:
            original = fh.readlines()
        key_written = False
        for line in original:
            if line.strip().startswith(f"{ENV_KEY}="):
                lines.append(f"{ENV_KEY}={token}\n")
                key_written = True
            else:
                lines.append(line)
        if not key_written:
            lines.append(f"{ENV_KEY}={token}\n")
    else:
        lines = [f"{ENV_KEY}={token}\n"]

    with open(ENV_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    print(f"✅ Token saved to {ENV_FILE}")


def _prompt_for_token() -> str:
    print()
    print("=" * 60)
    print("  🤖  Certification Quiz Bot — First-time Setup")
    print("=" * 60)
    print()
    print("No Telegram bot token was found.")
    print()
    print("How to get a token:")
    print("  1. Open Telegram and search for @BotFather")
    print("  2. Send /newbot and follow the instructions")
    print("  3. Copy the token (looks like 123456:ABC-DEF...)")
    print()
    while True:
        token = input("Paste your bot token here: ").strip()
        if not token:
            print("⚠️  Token cannot be empty. Try again.")
            continue
        if ":" not in token or len(token) < 20:
            print("⚠️  That doesn't look like a valid token. Try again.")
            continue
        return token


def resolve_token() -> str:
    """
    Returns a valid-looking token, prompting the user if necessary.
    Saves the token to .env if it was entered manually.
    """
    # 1. Check environment (already exported)
    token = os.environ.get(ENV_KEY, "").strip()
    if token:
        return token

    # 2. Check .env file
    env_vars = _read_env_file()
    token = env_vars.get(ENV_KEY, "").strip()
    if token:
        os.environ[ENV_KEY] = token   # make it available to child imports
        return token

    # 3. Prompt
    token = _prompt_for_token()
    _write_token_to_env(token)
    os.environ[ENV_KEY] = token
    return token


# ── logging setup ────────────────────────────────────────────────────────────

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")


def setup_logging() -> None:
    """
    Log to both the console (stdout) and a daily rotating file under logs/.
    File: logs/bot_YYYY-MM-DD.log
    Keeps the last 30 days automatically.
    """
    import logging.handlers
    from datetime import date

    os.makedirs(LOGS_DIR, exist_ok=True)

    log_file = os.path.join(LOGS_DIR, f"bot_{date.today()}.log")
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    # Rotating file handler — new file every day, keeps 30 days
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Reduce noise from httpx (Telegram HTTP calls)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    token = resolve_token()
    log.info("Bot starting up.")

    from src.telegram_bot import build_app

    app = build_app(token)

    print()
    print("🤖 Certification Quiz Bot is running. Press Ctrl+C to stop.")
    print(f"📄 Logs: {LOGS_DIR}")
    print()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
