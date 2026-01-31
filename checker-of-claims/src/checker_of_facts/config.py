from __future__ import annotations

import os

try:
    from dotenv import load_dotenv, find_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None
    find_dotenv = None

if load_dotenv is not None:
    dotenv_path = find_dotenv(usecwd=True) if find_dotenv is not None else ""
    load_dotenv(dotenv_path or None)

DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", ".workspace")
