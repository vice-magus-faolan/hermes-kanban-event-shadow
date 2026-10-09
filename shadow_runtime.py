#!/usr/bin/env python3
"""Durable, bounded shadow handling for selected native Kanban worker events.

This module deliberately owns no Kanban lifecycle transition.  A native hook
records a minimal signal; a separately invoked deterministic consumer
revalidates the canonical board before marking the signal observed.  It never
creates tasks, wakes agents, sends notifications, or runs commands from event
payloads.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

HANDLER_VERSION = "v1"
HOOK_EXITED = "on_kanban_worker_exited"
HOOK_STALE = "on_kanban_worker_stale_claim"
SUPPORTED_HOOKS = {HOOK_EXITED, HOOK_STALE}
VALID_RETRY_STATUSES = {"ready", "review"}
VALID_COMPARISONS = {"detected", "missed", "false_positive", "not_applicable"}
TRANSIENT_VALIDATION_OUTCOMES = {"board_unavailable", "board_schema_error"}
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_CAP_SECONDS = 15 * 60


def _now() -> int:
    return int(time.time())


def _absolute_normalized_regular_path(
    path: str | Path, field_name: str, *, allow_missing_final: bool
) -> Path:
    """Reject implicit-CWD, non-normalized, or non-regular durable paths."""
    raw_path = os.fspath(path)
    if isinstance(raw_path, bytes):
        raw_path = os.fsdecode(raw_path)
    candidate = Path(raw_path)
    if (
        not raw_path
        or not candidate.is_absolute()
        or os.path.normpath(raw_path) != raw_path
    ):
        raise ValueError(f"{field_name}_must_be_absolute_normalized")
    if any(component.is_symlink() for component in (candidate, *candidate.parents)):
        raise ValueError(f"{field_name}_must_be_regular_file")
    if candidate.exists() and not candidate.is_file():
        raise ValueError(f"{field_name}_must_be_regular_file")
    if not allow_missing_final and not candidate.is_file():
        raise ValueError(f"{field_name}_must_be_regular_file")
    return candidate


def validate_store_path(path: str | Path) -> Path:
    """Validate an explicitly configured private SQLite signal-store path."""
    return _absolute_normalized_regular_path(path, "store_path", allow_missing_final=True)


def validate_board_db_path(path: str | Path, *, allow_missing_final: bool = False) -> Path:
    """Validate a canonical board path, preserving unavailable-board recovery."""
    return _absolute_normalized_regular_path(
        path, "board_db", allow_missing_final=allow_missing_final
    )


def _require_private_store_parent(store: Path) -> None:
    """Require a pre-existing private directory to make path checks durable."""
    if not store.parent.is_dir():
        raise ValueError("store_parent_must_exist")
    for ancestor in (store.parent, *store.parent.parents):
        if stat.S_IMODE(ancestor.stat().st_mode) & 0o077 == 0:
            return
    raise ValueError("store_parent_must_be_private")


def _store_matches_descriptor(store: Path, descriptor_stat: os.stat_result) -> bool:
    """Return whether the configured pathname still names the opened store file."""
    try:
        path_stat = os.lstat(store)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(path_stat.st_mode)
        and (path_stat.st_dev, path_stat.st_ino)
        == (descriptor_stat.st_dev, descriptor_stat.st_ino)
    )


def _prepare_private_store(path: str | Path) -> tuple[Path, int, os.stat_result]:
    """Open a private store without following a final-path symlink.

    The descriptor stays open until SQLite has opened the same inode.  The
    pre-existing parent hierarchy must contain a private boundary, so an
    untrusted user cannot swap the pathname between the no-follow open and
    SQLite's pathname-based open.
    """
    store = validate_store_path(path)
    _require_private_store_parent(store)
    open_flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(store, open_flags)
    except FileNotFoundError:
        try:
            descriptor = os.open(store, open_flags | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                descriptor = os.open(store, open_flags)
            else:
                raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("store_path_must_be_regular_file") from None
        raise
    try:
        descriptor_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_uid != os.geteuid()
        ):
            raise ValueError("store_path_must_be_private_regular_file")
        os.fchmod(descriptor, 0o600)
        if not _store_matches_descriptor(store, descriptor_stat):
            raise ValueError("store_path_changed_during_open")
    except Exception:
        os.close(descriptor)
        raise
    return store, descriptor, descriptor_stat


def _resolved(path: str | Path, *, allow_missing_final: bool = False) -> str:
    return str(validate_board_db_path(path, allow_missing_final=allow_missing_final).resolve())


def board_fingerprint(board_db: str | Path, *, allow_missing_final: bool = False) -> str:
    """Return a stable local identity for a configured canonical board file."""
    return hashlib.sha256(
        _resolved(board_db, allow_missing_final=allow_missing_final).encode("utf-8")
    ).hexdigest()


def _connect_store(path: str | Path) -> sqlite3.Connection:
    store, descriptor, descriptor_stat = _prepare_private_store(path)
    try:
        conn = sqlite3.connect(store, timeout=0.05, isolation_level=None)
        if not _store_matches_descriptor(store, descriptor_stat):
            conn.close()
            raise ValueError("store_path_changed_during_open")
    finally:
        os.close(descriptor)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_store(path: str | Path) -> None:
    """Create the private signal store outside the lifecycle callback path."""
    with _connect_store(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_signals (
                source_key TEXT PRIMARY KEY,
                handler_version TEXT NOT NULL,
                board TEXT NOT NULL,
                board_db TEXT NOT NULL,
                board_fingerprint TEXT NOT NULL,
                task_id TEXT NOT NULL,
                run_id INTEGER NOT NULL,
                hook_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                retry_status TEXT NOT NULL,
                discovery_source TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'done', 'discarded')),
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                claim_token TEXT,
                claim_expires INTEGER,
                recorded_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                processed_outcome TEXT,
                supervisor_comparison TEXT NOT NULL DEFAULT 'unknown',
                last_error_class TEXT,
                retry_not_before INTEGER,
                source_event_id INTEGER,
                source_event_kind TEXT,
                source_event_created_at INTEGER,
                canonical_source_key TEXT,
                detected_at INTEGER,
                time_to_detection_seconds INTEGER,
                callback_recorded_at INTEGER,
                callback_observations INTEGER NOT NULL DEFAULT 0,
                callback_duplicate_suppressions INTEGER NOT NULL DEFAULT 0,
                audit_recorded_at INTEGER,
                audit_observations INTEGER NOT NULL DEFAULT 0,
                audit_duplicate_suppressions INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS shadow_signals_pending
                ON shadow_signals(state, recorded_at);
            CREATE TABLE IF NOT EXISTS shadow_cursors (
                board_fingerprint TEXT PRIMARY KEY,
                last_event_id INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );
            """
        )
        existing_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(shadow_signals)")
        }
        migrations = {
            "source_event_id": "INTEGER",
            "source_event_kind": "TEXT",
            "source_event_created_at": "INTEGER",
            "canonical_source_key": "TEXT",
            "detected_at": "INTEGER",
            "time_to_detection_seconds": "INTEGER",
            "callback_recorded_at": "INTEGER",
            "callback_observations": "INTEGER NOT NULL DEFAULT 0",
            "callback_duplicate_suppressions": "INTEGER NOT NULL DEFAULT 0",
            "audit_recorded_at": "INTEGER",
            "audit_observations": "INTEGER NOT NULL DEFAULT 0",
            "audit_duplicate_suppressions": "INTEGER NOT NULL DEFAULT 0",
            "retry_not_before": "INTEGER",
            "claim_token": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE shadow_signals ADD COLUMN {column} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS shadow_signals_board_due "
            "ON shadow_signals(board_fingerprint, state, retry_not_before, recorded_at)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS shadow_signals_canonical_event "
            "ON shadow_signals(board_fingerprint, source_event_id, handler_version) "
            "WHERE source_event_id IS NOT NULL"
        )
        conn.execute(
            """
            UPDATE shadow_signals
               SET callback_recorded_at = recorded_at, callback_observations = 1
             WHERE discovery_source = 'callback' AND callback_recorded_at IS NULL
            """
        )
        conn.execute(
            """
            UPDATE shadow_signals
               SET audit_recorded_at = recorded_at, audit_observations = 1
             WHERE discovery_source = 'audit' AND audit_recorded_at IS NULL
            """
        )
        conn.execute(
            "INSERT INTO shadow_meta(key, value) VALUES ('schema_version', '4') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )


def _source_key(
    *, board_db: str | Path, task_id: str, run_id: int, hook_name: str
) -> str:
    # Native worker hooks do not expose task_events.id.  A run can produce each
    # selected hook at most once, so this generation tuple is the callback-side
    # idempotency key.  The consumer then finds and validates its durable event.
    material = "\x1f".join(
        (HANDLER_VERSION, board_fingerprint(board_db), task_id, str(run_id), hook_name)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _canonical_source_key(
    *, board_fingerprint_value: str, event_id: int, task_id: str, run_id: int, kind: str
) -> str:
    """Return an auditable identity for one immutable canonical event tuple."""
    material = "\x1f".join(
        (HANDLER_VERSION, board_fingerprint_value, str(event_id), task_id, str(run_id), kind)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _observation_columns(discovery_source: str) -> tuple[str, str, str]:
    """Return durable evidence columns for one fixed recorder source."""
    if discovery_source == "callback":
        return (
            "callback_recorded_at",
            "callback_observations",
            "callback_duplicate_suppressions",
        )
    if discovery_source == "audit":
        return (
            "audit_recorded_at",
            "audit_observations",
            "audit_duplicate_suppressions",
        )
    raise ValueError("invalid_discovery_source")


def _record_duplicate_observation(
    conn: sqlite3.Connection, source_key: str, discovery_source: str, timestamp: int
) -> None:
    """Persist duplicate suppression rather than returning a transient result only."""
    _, observations, suppressions = _observation_columns(discovery_source)
    conn.execute(
        f"""
        UPDATE shadow_signals
           SET {observations} = {observations} + 1,
               {suppressions} = {suppressions} + 1,
               updated_at = ?
         WHERE source_key = ?
        """,
        (timestamp, source_key),
    )


def _validate_source_event(
    *, event_id: int | None, kind: str | None, created_at: int | None, expected_kinds: tuple[str, ...]
) -> None:
    values = (event_id, kind, created_at)
    if all(value is None for value in values):
        return
    if (
        isinstance(event_id, bool)
        or not isinstance(event_id, int)
        or event_id <= 0
        or not isinstance(kind, str)
        or kind not in expected_kinds
        or isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or created_at <= 0
    ):
        raise ValueError("invalid_source_event")


def _validate_hook_fields(
    hook_name: str,
    board: Any,
    board_db: str | Path,
    task_id: Any,
    run_id: Any,
    retry_status: Any,
    outcome: Any,
) -> tuple[str, int, str, str]:
    if hook_name not in SUPPORTED_HOOKS:
        raise ValueError("unsupported_hook")
    if not isinstance(board, str) or not board:
        raise ValueError("invalid_board")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("invalid_task_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("invalid_run_id")
    if retry_status not in VALID_RETRY_STATUSES:
        raise ValueError("invalid_retry_status")
    if hook_name == HOOK_EXITED and outcome not in {"crashed", "rate_limited"}:
        raise ValueError("invalid_exit_outcome")
    if hook_name == HOOK_STALE:
        outcome = "reclaimed"
    return task_id, run_id, retry_status, str(outcome)


def record_hook_signal(
    *,
    store_path: str | Path,
    board: Any,
    board_db: str | Path,
    hook_name: str,
    task_id: Any,
    run_id: Any,
    retry_status: Any,
    outcome: Any = None,
    discovery_source: str = "callback",
    source_event_id: int | None = None,
    source_event_kind: str | None = None,
    source_event_created_at: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Record one callback signal using a unique generation key.

    This function intentionally does not open the board DB.  It performs only
    bounded local validation plus one INSERT OR IGNORE into the preinitialized
    signal store; expensive canonical-state reads are deferred to consume_one.
    """
    task_id, run_id, retry_status, outcome = _validate_hook_fields(
        hook_name, board, board_db, task_id, run_id, retry_status, outcome
    )
    expected_kinds = _expected_event_kinds({"hook_name": hook_name, "outcome": outcome})
    _validate_source_event(
        event_id=source_event_id,
        kind=source_event_kind,
        created_at=source_event_created_at,
        expected_kinds=expected_kinds,
    )
    timestamp = _now() if now is None else now
    recorded_at_column, observations_column, _ = _observation_columns(discovery_source)
    resolved_db = _resolved(board_db)
    fingerprint = board_fingerprint(resolved_db)
    source_key = _source_key(
        board_db=resolved_db, task_id=task_id, run_id=run_id, hook_name=hook_name
    )
    canonical_key: str | None = None
    if source_event_id is not None:
        assert source_event_kind is not None
        canonical_key = _canonical_source_key(
            board_fingerprint_value=fingerprint,
            event_id=source_event_id,
            task_id=task_id,
            run_id=run_id,
            kind=source_event_kind,
        )
    with _connect_store(store_path) as conn:
        if canonical_key is not None:
            canonical_existing = conn.execute(
                "SELECT source_key FROM shadow_signals WHERE canonical_source_key = ?",
                (canonical_key,),
            ).fetchone()
            if canonical_existing is not None:
                _record_duplicate_observation(
                    conn, str(canonical_existing["source_key"]), discovery_source, timestamp
                )
                return {"source_key": str(canonical_existing["source_key"]), "recorded": False}
        existing = conn.execute(
            "SELECT source_key, source_event_id FROM shadow_signals WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if existing is not None:
            _record_duplicate_observation(conn, source_key, discovery_source, timestamp)
            if source_event_id is not None and existing["source_event_id"] is None:
                conn.execute(
                    """
                    UPDATE shadow_signals
                       SET source_event_id = ?, source_event_kind = ?,
                           source_event_created_at = ?, canonical_source_key = ?, updated_at = ?
                     WHERE source_key = ? AND source_event_id IS NULL
                    """,
                    (
                        source_event_id,
                        source_event_kind,
                        source_event_created_at,
                        canonical_key,
                        timestamp,
                        source_key,
                    ),
                )
            return {"source_key": source_key, "recorded": False}
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO shadow_signals (
                source_key, handler_version, board, board_db, board_fingerprint,
                task_id, run_id, hook_name, outcome, retry_status, discovery_source,
                state, recorded_at, updated_at, source_event_id, source_event_kind,
                source_event_created_at, canonical_source_key, callback_recorded_at,
                callback_observations, callback_duplicate_suppressions, audit_recorded_at,
                audit_observations, audit_duplicate_suppressions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                HANDLER_VERSION,
                str(board),
                resolved_db,
                fingerprint,
                task_id,
                run_id,
                hook_name,
                outcome,
                retry_status,
                discovery_source,
                timestamp,
                timestamp,
                source_event_id,
                source_event_kind,
                source_event_created_at,
                canonical_key,
                timestamp if recorded_at_column == "callback_recorded_at" else None,
                1 if observations_column == "callback_observations" else 0,
                0,
                timestamp if recorded_at_column == "audit_recorded_at" else None,
                1 if observations_column == "audit_observations" else 0,
                0,
            ),
        )
        if cursor.rowcount != 1:
            conflict = conn.execute(
                "SELECT source_key FROM shadow_signals WHERE source_key = ? "
                "OR canonical_source_key = ? LIMIT 1",
                (source_key, canonical_key),
            ).fetchone()
            if conflict is not None:
                conflict_key = str(conflict["source_key"])
                _record_duplicate_observation(conn, conflict_key, discovery_source, timestamp)
                return {"source_key": conflict_key, "recorded": False}
    return {"source_key": source_key, "recorded": cursor.rowcount == 1}


def _claim_next(
    store_path: str | Path,
    board_fingerprint_value: str,
    owner: str,
    claim_token: str,
    now: int,
    lease_seconds: int,
) -> dict[str, Any] | None:
    with _connect_store(store_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT * FROM shadow_signals
                WHERE board_fingerprint = ?
                  AND ((state = 'pending'
                        AND (retry_not_before IS NULL OR retry_not_before <= ?))
                    OR (state = 'claimed' AND claim_expires IS NOT NULL AND claim_expires < ?))
                ORDER BY recorded_at, source_key LIMIT 1
                """,
                (board_fingerprint_value, now, now),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            source_key = str(row["source_key"])
            cursor = conn.execute(
                """
                UPDATE shadow_signals
                   SET state = 'claimed', claimed_by = ?, claim_token = ?, claim_expires = ?,
                       attempts = attempts + 1, updated_at = ?
                 WHERE source_key = ? AND board_fingerprint = ?
                   AND ((state = 'pending'
                         AND (retry_not_before IS NULL OR retry_not_before <= ?))
                     OR (state = 'claimed' AND claim_expires IS NOT NULL AND claim_expires < ?))
                """,
                (
                    owner, claim_token, now + lease_seconds, now, source_key, board_fingerprint_value,
                    now, now,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = dict(conn.execute(
                "SELECT * FROM shadow_signals WHERE source_key = ?", (source_key,)
            ).fetchone())
            conn.execute("COMMIT")
            return claimed
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _expected_event_kinds(signal: dict[str, Any]) -> tuple[str, ...]:
    if signal["hook_name"] == HOOK_STALE:
        return ("reclaimed",)
    if signal["outcome"] == "rate_limited":
        return ("rate_limited",)
    # A clean worker exit without its terminal lifecycle call is a durable
    # protocol_violation but still has run outcome crashed.
    return ("crashed", "protocol_violation")


def _is_selected_reconciliation_event(event: sqlite3.Row) -> bool:
    """Exclude durable events that cannot have emitted a selected native hook."""
    if event["kind"] != "reclaimed":
        return True
    payload = event["payload"]
    if payload is None:
        return True
    try:
        decoded = json.loads(str(payload))
    except (TypeError, ValueError):
        return False
    return not (isinstance(decoded, dict) and decoded.get("manual") is True)


def _source_retry_status(event: sqlite3.Row) -> str | None:
    """Return the immutable retry phase emitted with a selected native event.

    The current task status can already belong to a successor run by the time
    the audit observes an event.  Reconciliation must preserve the source
    phase so the consumer can record and then classify that event as
    superseded rather than leaving the cursor behind it.
    """
    payload = event["payload"]
    if payload is None:
        return None
    try:
        decoded = json.loads(str(payload))
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    retry_status = decoded.get("retry_status")
    return retry_status if retry_status in VALID_RETRY_STATUSES else None


def _validate_canonical_signal(
    signal: dict[str, Any], board_db: str | Path
) -> tuple[str, dict[str, int | str] | None]:
    if _resolved(board_db, allow_missing_final=True) != signal["board_db"]:
        return "board_mismatch", None
    uri = f"file:{Path(board_db).expanduser().resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return "board_unavailable", None
    try:
        task = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (signal["task_id"],)
        ).fetchone()
        if task is None:
            return "missing_task", None
        if task["current_run_id"] is not None or task["status"] != signal["retry_status"]:
            return "superseded", None
        run = conn.execute(
            "SELECT task_id, outcome, ended_at FROM task_runs WHERE id = ?", (signal["run_id"],)
        ).fetchone()
        if (
            run is None
            or run["task_id"] != signal["task_id"]
            or run["outcome"] != signal["outcome"]
            or run["ended_at"] is None
        ):
            return "run_mismatch", None
        kinds = _expected_event_kinds(signal)
        placeholders = ",".join("?" for _ in kinds)
        source_event_id = signal.get("source_event_id")
        event_predicate = ""
        event_parameters: tuple[Any, ...] = ()
        if source_event_id is not None:
            event_predicate = " AND id = ?"
            event_parameters = (int(source_event_id),)
        event = conn.execute(
            f"SELECT id, kind, created_at FROM task_events WHERE task_id = ? AND run_id = ? "
            f"AND kind IN ({placeholders}){event_predicate} ORDER BY id DESC LIMIT 1",
            (signal["task_id"], signal["run_id"], *kinds, *event_parameters),
        ).fetchone()
        if event is None:
            return "missing_durable_event", None
        if source_event_id is not None and (
            signal["source_event_kind"] != event["kind"]
            or int(signal["source_event_created_at"]) != int(event["created_at"])
        ):
            return "source_event_mismatch", None
        return "detected_current", {
            "id": int(event["id"]),
            "kind": str(event["kind"]),
            "created_at": int(event["created_at"]),
        }
    except sqlite3.Error:
        return "board_schema_error", None
    finally:
        conn.close()


def _bind_canonical_event(
    store_path: str | Path,
    signal: dict[str, Any],
    owner: str,
    event: dict[str, int | str],
    now: int,
) -> bool:
    """Bind a claimed signal to its immutable canonical event before completion."""
    event_id = int(event["id"])
    event_kind = str(event["kind"])
    event_created_at = int(event["created_at"])
    canonical_key = _canonical_source_key(
        board_fingerprint_value=str(signal["board_fingerprint"]),
        event_id=event_id,
        task_id=str(signal["task_id"]),
        run_id=int(signal["run_id"]),
        kind=event_kind,
    )
    with _connect_store(store_path) as conn:
        try:
            return conn.execute(
                """
                UPDATE shadow_signals
                   SET source_event_id = ?, source_event_kind = ?,
                       source_event_created_at = ?, canonical_source_key = ?,
                       detected_at = ?, time_to_detection_seconds = ?, updated_at = ?
                 WHERE source_key = ? AND state = 'claimed' AND claimed_by = ?
                   AND claim_token = ?
                   AND (source_event_id IS NULL OR source_event_id = ?)
                """,
                (
                    event_id,
                    event_kind,
                    event_created_at,
                    canonical_key,
                    now,
                    max(0, now - event_created_at),
                    now,
                    signal["source_key"],
                    owner,
                    signal["claim_token"],
                    event_id,
                ),
            ).rowcount == 1
        except sqlite3.IntegrityError:
            return False


def _finish_signal(
    store_path: str | Path, signal: dict[str, Any], owner: str, outcome: str, now: int
) -> bool:
    terminal_state = "done" if outcome == "detected_current" else "discarded"
    with _connect_store(store_path) as conn:
        return conn.execute(
            """
            UPDATE shadow_signals
               SET state = ?, processed_outcome = ?, claimed_by = NULL,
                   claim_token = NULL, claim_expires = NULL, updated_at = ?,
                   retry_not_before = NULL,
                   last_error_class = CASE WHEN ? = 'detected_current' THEN NULL ELSE ? END
             WHERE source_key = ? AND state = 'claimed' AND claimed_by = ?
               AND claim_token = ?
            """,
            (
                terminal_state, outcome, now, outcome, outcome, signal["source_key"], owner,
                signal["claim_token"],
            ),
        ).rowcount == 1


def _retry_delay_seconds(attempts: int) -> int:
    """Return the capped deterministic backoff for a claimed signal attempt."""
    exponent = min(max(attempts - 1, 0), 5)
    return min(RETRY_BACKOFF_CAP_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (2 ** exponent))


def _release_for_retry(
    store_path: str | Path, signal: dict[str, Any], owner: str, outcome: str, now: int
) -> bool:
    """Release a transient canonical-board failure for a later bounded retry."""
    retry_not_before = now + _retry_delay_seconds(int(signal["attempts"]))
    with _connect_store(store_path) as conn:
        return conn.execute(
            """
            UPDATE shadow_signals
               SET state = 'pending', processed_outcome = 'retry_scheduled',
                   claimed_by = NULL, claim_token = NULL, claim_expires = NULL, updated_at = ?,
                   last_error_class = ?, retry_not_before = ?
             WHERE source_key = ? AND state = 'claimed' AND claimed_by = ?
               AND claim_token = ?
            """,
            (now, outcome, retry_not_before, signal["source_key"], owner, signal["claim_token"]),
        ).rowcount == 1


def consume_one(
    *, store_path: str | Path, board_db: str | Path, owner: str,
    lease_seconds: int = 60, now: int | None = None,
) -> dict[str, Any] | None:
    """Handle exactly one signal without mutating the Kanban board."""
    timestamp = _now() if now is None else now
    claim_token = uuid.uuid4().hex
    signal = _claim_next(
        store_path,
        board_fingerprint(board_db, allow_missing_final=True),
        owner,
        claim_token,
        timestamp,
        lease_seconds,
    )
    if signal is None:
        return None
    outcome, event = _validate_canonical_signal(signal, board_db)
    if outcome in TRANSIENT_VALIDATION_OUTCOMES:
        _release_for_retry(store_path, signal, owner, outcome, timestamp)
        return {"source_key": signal["source_key"], "outcome": outcome}
    if event is not None and not _bind_canonical_event(store_path, signal, owner, event, timestamp):
        outcome = "canonical_identity_conflict"
    _finish_signal(store_path, signal, owner, outcome, timestamp)
    return {"source_key": signal["source_key"], "outcome": outcome}


def reconcile_events(
    *, store_path: str | Path, board: str, board_db: str | Path, now: int | None = None,
) -> dict[str, int]:
    """Recover missed callbacks by scanning durable worker-exit/reclaim events."""
    timestamp = _now() if now is None else now
    resolved_db = _resolved(board_db)
    fingerprint = board_fingerprint(resolved_db)
    with _connect_store(store_path) as store:
        cursor_row = store.execute(
            "SELECT last_event_id FROM shadow_cursors WHERE board_fingerprint = ?", (fingerprint,)
        ).fetchone()
        last_event_id = int(cursor_row["last_event_id"]) if cursor_row else 0
    uri = f"file:{Path(resolved_db)}?mode=ro"
    board_conn = sqlite3.connect(uri, uri=True)
    board_conn.row_factory = sqlite3.Row
    try:
        events = board_conn.execute(
            """
            SELECT e.id, e.task_id, e.run_id, e.kind, e.created_at, e.payload, r.outcome
              FROM task_events e
              JOIN task_runs r ON r.id = e.run_id
             WHERE e.id > ? AND e.kind IN ('reclaimed', 'crashed', 'protocol_violation', 'rate_limited')
             ORDER BY e.id
            """,
            (last_event_id,),
        ).fetchall()
    finally:
        board_conn.close()
    scanned = recorded = duplicates = excluded = 0
    highest = last_event_id
    for event in events:
        highest = max(highest, int(event["id"]))
        if not _is_selected_reconciliation_event(event):
            excluded += 1
            continue
        retry_status = _source_retry_status(event)
        if retry_status is None:
            excluded += 1
            continue
        scanned += 1
        hook_name = HOOK_STALE if event["kind"] == "reclaimed" else HOOK_EXITED
        result = record_hook_signal(
            store_path=store_path,
            board=board,
            board_db=resolved_db,
            hook_name=hook_name,
            task_id=event["task_id"],
            run_id=int(event["run_id"]),
            retry_status=retry_status,
            outcome=event["outcome"],
            discovery_source="audit",
            source_event_id=int(event["id"]),
            source_event_kind=str(event["kind"]),
            source_event_created_at=int(event["created_at"]),
            now=timestamp,
        )
        if result["recorded"]:
            recorded += 1
        else:
            duplicates += 1
    with _connect_store(store_path) as store:
        store.execute(
            """
            INSERT INTO shadow_cursors(board_fingerprint, last_event_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(board_fingerprint) DO UPDATE SET
                last_event_id = excluded.last_event_id, updated_at = excluded.updated_at
            """,
            (fingerprint, highest, timestamp),
        )
    return {
        "scanned": scanned,
        "recorded": recorded,
        "duplicates": duplicates,
        "excluded": excluded,
    }


def record_comparison(store_path: str | Path, source_key: str, comparison: str) -> bool:
    """Record an operator's supervisor-parity observation for one signal."""
    if comparison not in VALID_COMPARISONS:
        raise ValueError("invalid_comparison")
    with _connect_store(store_path) as conn:
        return conn.execute(
            "UPDATE shadow_signals SET supervisor_comparison = ?, updated_at = ? WHERE source_key = ?",
            (comparison, _now(), source_key),
        ).rowcount == 1


def shadow_summary(store_path: str | Path) -> dict[str, Any]:
    """Return aggregate shadow evidence, including explicit parity classifications."""
    with _connect_store(store_path) as conn:
        rows = conn.execute(
            """
            SELECT board_fingerprint, task_id, run_id, state, processed_outcome,
                   supervisor_comparison, source_event_id, source_event_kind,
                   source_event_created_at, detected_at, time_to_detection_seconds,
                   callback_recorded_at, callback_observations,
                   callback_duplicate_suppressions, audit_recorded_at,
                   audit_observations, audit_duplicate_suppressions, attempts,
                   last_error_class, retry_not_before
              FROM shadow_signals
             ORDER BY board_fingerprint, source_event_id, task_id, run_id, source_key
            """
        ).fetchall()
    detection_delays = [
        int(row["time_to_detection_seconds"])
        for row in rows
        if row["time_to_detection_seconds"] is not None
    ]
    eligible_event_evidence = [
        {
            "board_fingerprint": str(row["board_fingerprint"]),
            "source_event_id": int(row["source_event_id"]),
            "task_id": str(row["task_id"]),
            "run_id": int(row["run_id"]),
            "source_event_kind": str(row["source_event_kind"]),
            "state": str(row["state"]),
            "recovery": {
                "attempts": int(row["attempts"]),
                "last_error_class": row["last_error_class"],
                "retry_not_before": row["retry_not_before"],
            },
            "callback": {
                "recorded_at": row["callback_recorded_at"],
                "observations": int(row["callback_observations"]),
                "duplicate_suppressions": int(row["callback_duplicate_suppressions"]),
            },
            "audit": {
                "recorded_at": row["audit_recorded_at"],
                "observations": int(row["audit_observations"]),
                "duplicate_suppressions": int(row["audit_duplicate_suppressions"]),
            },
        }
        for row in rows
        if row["source_event_id"] is not None
    ]
    return {
        "signals": len(rows),
        "eligible_events": sum(row["source_event_id"] is not None for row in rows),
        "states": dict(Counter(str(row["state"]) for row in rows)),
        "outcomes": dict(Counter(str(row["processed_outcome"] or "pending") for row in rows)),
        "supervisor_comparison": dict(Counter(str(row["supervisor_comparison"]) for row in rows)),
        "false_positives": sum(row["supervisor_comparison"] == "false_positive" for row in rows),
        "missed_cases": sum(row["supervisor_comparison"] == "missed" for row in rows),
        "time_to_detection": {
            "count": len(detection_delays),
            "min_seconds": min(detection_delays) if detection_delays else None,
            "max_seconds": max(detection_delays) if detection_delays else None,
            "total_seconds": sum(detection_delays),
        },
        "observation_evidence": {
            source: {
                "events_recorded": sum(row[f"{source}_recorded_at"] is not None for row in rows),
                "observations": sum(int(row[f"{source}_observations"]) for row in rows),
                "duplicate_suppressions": sum(
                    int(row[f"{source}_duplicate_suppressions"]) for row in rows
                ),
            }
            for source in ("callback", "audit")
        },
        "eligible_event_evidence": eligible_event_evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("consume", "reconcile"):
        command = subparsers.add_parser(name)
        command.add_argument("--store", required=True)
        command.add_argument("--board-db", required=True)
        if name == "consume":
            command.add_argument("--owner", required=True)
        else:
            command.add_argument("--board", required=True)
    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("--store", required=True)
    comparison_parser = subparsers.add_parser("record-comparison")
    comparison_parser.add_argument("--store", required=True)
    comparison_parser.add_argument("--source-key", required=True)
    comparison_parser.add_argument("--comparison", choices=sorted(VALID_COMPARISONS), required=True)
    args = parser.parse_args()
    try:
        if args.command in {"consume", "reconcile"}:
            validate_board_db_path(args.board_db)
        initialize_store(args.store)
        if args.command == "consume":
            print(json.dumps(consume_one(store_path=args.store, board_db=args.board_db, owner=args.owner), sort_keys=True))
        elif args.command == "reconcile":
            print(json.dumps(reconcile_events(store_path=args.store, board=args.board, board_db=args.board_db), sort_keys=True))
        elif args.command == "summary":
            print(json.dumps(shadow_summary(args.store), sort_keys=True))
        else:
            print(json.dumps({"updated": record_comparison(args.store, args.source_key, args.comparison)}))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
