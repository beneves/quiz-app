"""
Entry point for the Certification Quiz Discord bot.

Token resolution order:
  1. Already set in environment (DISCORD_TOKEN)
  2. Exists in .env file
  3. Prompt the user to enter it → saved to .env
"""
from __future__ import annotations

import logging
import os
import sys

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
ENV_KEY  = "DISCORD_TOKEN"


def _read_env_file() -> dict[str, str]:
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
    lines = []
    key_written = False
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as fh:
            original = fh.readlines()
        for line in original:
            if line.strip().startswith(f"{ENV_KEY}="):
                lines.append(f"{ENV_KEY}={token}\n")
                key_written = True
            else:
                lines.append(line)
    if not key_written:
        lines.append(f"{ENV_KEY}={token}\n")
    with open(ENV_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    print(f"✅ Token saved to {ENV_FILE}")


def _prompt_for_token() -> str:
    print()
    print("=" * 60)
    print("  🤖  Certification Quiz Bot — Discord Setup")
    print("=" * 60)
    print()
    print("No Discord bot token was found.")
    print()
    print("How to get a token:")
    print("  1. Go to https://discord.com/developers/applications")
    print("  2. Open your application → Bot → Reset Token")
    print("  3. Copy and paste the token below")
    print()
    while True:
        token = input("Paste your Discord bot token here: ").strip()
        if not token:
            print("⚠️  Token cannot be empty. Try again.")
            continue
        if len(token) < 50:
            print("⚠️  That doesn't look like a valid Discord token. Try again.")
            continue
        return token


def resolve_token() -> str:
    token = os.environ.get(ENV_KEY, "").strip()
    if token:
        return token

    env_vars = _read_env_file()
    token = env_vars.get(ENV_KEY, "").strip()
    if token:
        os.environ[ENV_KEY] = token
        return token

    token = _prompt_for_token()
    _write_token_to_env(token)
    os.environ[ENV_KEY] = token
    return token


# ── logging ───────────────────────────────────────────────────────────────────

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")


def setup_logging() -> None:
    import logging.handlers
    from datetime import date

    os.makedirs(LOGS_DIR, exist_ok=True)
    log_file = os.path.join(LOGS_DIR, f"discord_{date.today()}.log")
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    token = resolve_token()
    log.info("Discord bot starting up.")

    from src.discord_bot import build_bot

    bot = build_bot(token)

    print()
    print("🤖 Certification Quiz Discord Bot is running. Press Ctrl+C to stop.")
    print(f"📄 Logs: {LOGS_DIR}")
    print()
    bot.run(token)


if __name__ == "__main__":
    main()
