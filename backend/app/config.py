"""Runtime settings (environment overridable). Everything is local by default."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def db_path() -> str:
    return os.environ.get("MINI_OBD_DB") or str(Path.home() / ".mini-obd" / "workbench.sqlite3")


def fixtures_dir() -> Path:
    return Path(os.environ.get("MINI_OBD_FIXTURES") or REPO_ROOT / "fixtures")


def frontend_dist() -> Path:
    return Path(os.environ.get("MINI_OBD_FRONTEND") or REPO_ROOT / "frontend" / "dist")


API_HOST = os.environ.get("MINI_OBD_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("MINI_OBD_API_PORT", "8765"))
