"""
Central configuration for the WhatsApp status bot.
Everything is controlled via environment variables so the same code
runs unchanged locally and on Railway.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# Automatically load variables from a .env file in the working directory,
# if one exists. This makes `.env` work the same way on Windows
# (PowerShell/cmd), macOS, and Linux, without needing shell-specific
# export syntax. Real environment variables (e.g. ones Railway injects)
# always take priority and are never overwritten by the .env file.
load_dotenv()


def _bool(env_name: str, default: bool) -> bool:
    val = os.getenv(env_name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(env_name: str, default: float) -> float:
    val = os.getenv(env_name)
    if val is None or not val.strip():
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_session_path() -> str:
    # Resolve relative to the project root (parent of this app/ folder),
    # not the current working directory — so the session file is found
    # in the same place no matter where `python main.py` is launched from.
    return os.path.join(_project_root(), "data", "session.db3")


class Config:
    # Phone number to pair, digits only with country code, no "+" and no spaces.
    # Example: 15551234567 for a US number.
    PHONE_NUMBER: str = os.getenv("PHONE_NUMBER", "").strip().lstrip("+")

    # Where the WhatsApp session (sqlite) is stored. Defaults to a stable
    # folder next to this project, so restarting the app locally reuses the
    # same session instead of asking you to re-pair every time. On Railway,
    # set this to a path inside an attached Volume (e.g. /data/session.db3)
    # so it survives redeploys too.
    SESSION_DB_PATH: str = os.getenv("SESSION_DB_PATH", _default_session_path())

    # Feature toggles.
    VIEW_STATUSES: bool = _bool("VIEW_STATUSES", True)
    LIKE_STATUSES: bool = _bool("LIKE_STATUSES", True)

    # Emoji used to react to ("like") a status.
    REACTION_EMOJI: str = os.getenv("REACTION_EMOJI", "❤️")

    # Randomized delay (seconds) before reacting, to avoid firing reactions
    # instantly/mechanically for every single status.
    MIN_REACT_DELAY_SECONDS: float = _float("MIN_REACT_DELAY_SECONDS", 2.0)
    MAX_REACT_DELAY_SECONDS: float = _float("MAX_REACT_DELAY_SECONDS", 8.0)

    # Optional whitelist: comma-separated phone numbers (digits only, no "+").
    # If set, only statuses from these numbers are viewed/liked.
    # If empty, every status the account can see is processed.
    ALLOWED_STATUS_SENDERS: list[str] = [
        s.strip().lstrip("+")
        for s in os.getenv("ALLOWED_STATUS_SENDERS", "").split(",")
        if s.strip()
    ]

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Log rotation: keeps a file transcript on disk that periodically
    # clears itself out instead of growing forever. Console/stdout output
    # (what Railway's own log dashboard shows) is unaffected either way.
    LOG_TO_FILE: bool = _bool("LOG_TO_FILE", True)
    LOG_FILE_PATH: str = os.getenv("LOG_FILE_PATH", os.path.join(_project_root(), "logs", "app.log"))
    LOG_ROTATE_HOURS: float = _float("LOG_ROTATE_HOURS", 6.0)
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "3"))

    # Periodically restart the whole process for a clean session (fresh
    # memory, fresh connection) — skips pairing since the session file is
    # already saved. 0 disables this entirely.
    AUTO_RESTART_HOURS: float = _float("AUTO_RESTART_HOURS", 6.0)

    # When the bot successfully connects, send a confirmation message to
    # the connected account's own WhatsApp inbox ("Message Yourself").
    NOTIFY_ON_STARTUP: bool = _bool("NOTIFY_ON_STARTUP", True)
    STARTUP_NOTIFICATION_MESSAGE: str = os.getenv(
        "STARTUP_NOTIFICATION_MESSAGE", "> 🟢 ESSENCE AUTO CONNECTED"
    )

    # Railway injects PORT automatically for web-type services.
    PORT: int = int(os.getenv("PORT", "8080"))

    @classmethod
    def validate(cls) -> None:
        if cls.MIN_REACT_DELAY_SECONDS < 0 or cls.MAX_REACT_DELAY_SECONDS < cls.MIN_REACT_DELAY_SECONDS:
            raise SystemExit(
                "MIN_REACT_DELAY_SECONDS / MAX_REACT_DELAY_SECONDS are invalid "
                "(min must be >= 0 and <= max)"
            )
        if cls.LOG_ROTATE_HOURS < 0 or cls.AUTO_RESTART_HOURS < 0 or cls.LOG_BACKUP_COUNT < 0:
            raise SystemExit(
                "LOG_ROTATE_HOURS, AUTO_RESTART_HOURS, and LOG_BACKUP_COUNT must all be >= 0"
            )
