"""Opt-in Hermes plugin for the shadow event-reaction prototype.

No configuration is changed by this repository artifact. The plugin remains
inert unless an operator supplies both a private store path and an explicit
board-to-canonical-DB mapping through its process environment.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "kanban_event_shadow_runtime", ROOT / "shadow_runtime.py"
)
if RUNTIME_SPEC is None or RUNTIME_SPEC.loader is None:
    raise RuntimeError("cannot load kanban_event_shadow runtime")
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
sys.modules[RUNTIME_SPEC.name] = RUNTIME
RUNTIME_SPEC.loader.exec_module(RUNTIME)
HOOK_EXITED = RUNTIME.HOOK_EXITED
HOOK_STALE = RUNTIME.HOOK_STALE
initialize_store = RUNTIME.initialize_store
record_hook_signal = RUNTIME.record_hook_signal
validate_board_db_path = RUNTIME.validate_board_db_path
validate_store_path = RUNTIME.validate_store_path


class ShadowRecorder:
    """Preconfigured, best-effort callback adapter with no board mutation."""

    def __init__(self, store_path: Path, board_databases: dict[str, str]) -> None:
        self.store_path = store_path
        self.board_databases = board_databases

    @classmethod
    def from_environment(cls) -> "ShadowRecorder | None":
        store = os.environ.get("HERMES_EVENT_SHADOW_STORE", "").strip()
        mapping = os.environ.get("HERMES_EVENT_SHADOW_BOARD_DATABASES", "").strip()
        if not store or not mapping:
            return None
        try:
            store_path = validate_store_path(store)
            raw_mapping = json.loads(mapping)
            if not isinstance(raw_mapping, dict) or not all(
                isinstance(board, str) and isinstance(database, str) and board and database
                for board, database in raw_mapping.items()
            ):
                raise ValueError("mapping must be a JSON object of non-empty string paths")
            board_databases = {
                board: str(validate_board_db_path(database))
                for board, database in raw_mapping.items()
            }
            initialize_store(store_path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            logger.warning("kanban_event_shadow disabled: invalid configuration: %s", exc)
            return None
        return cls(store_path, board_databases)

    def record(self, hook_name: str, **kwargs: Any) -> None:
        board = kwargs.get("board")
        board_db = self.board_databases.get(board) if isinstance(board, str) else None
        if board_db is None:
            return
        try:
            record_hook_signal(
                store_path=self.store_path,
                board=board,
                board_db=board_db,
                hook_name=hook_name,
                task_id=kwargs.get("task_id"),
                run_id=kwargs.get("run_id"),
                retry_status=kwargs.get("retry_status"),
                outcome=kwargs.get("outcome"),
            )
        except Exception as exc:  # Observer callbacks must never affect dispatch.
            logger.warning("kanban_event_shadow signal record failed: %s", exc)


def register(ctx: Any) -> None:
    """Register only bounded post-commit observers when explicitly configured."""
    recorder = ShadowRecorder.from_environment()
    if recorder is None:
        return
    ctx.register_hook(HOOK_EXITED, lambda **kwargs: recorder.record(HOOK_EXITED, **kwargs))
    ctx.register_hook(HOOK_STALE, lambda **kwargs: recorder.record(HOOK_STALE, **kwargs))
