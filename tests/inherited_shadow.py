from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "shadow_runtime.py"
spec = importlib.util.spec_from_file_location("kanban_event_shadow", SCRIPT)
assert spec is not None and spec.loader is not None
shadow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shadow
spec.loader.exec_module(shadow)


def create_board(
    path: Path,
    *,
    status: str = "ready",
    current_run_id: int | None = None,
    retry_status: str = "ready",
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT,
            created_at INTEGER NOT NULL, payload TEXT
        );
        """
    )
    conn.execute("INSERT INTO tasks VALUES ('t_event', ?, ?)", (status, current_run_id))
    conn.execute("INSERT INTO task_runs VALUES (9, 't_event', 'crashed', 10)")
    conn.execute(
        "INSERT INTO task_events VALUES (21, 't_event', 9, 'crashed', 10, ?)",
        (json.dumps({"retry_status": retry_status}),),
    )
    conn.commit()
    conn.close()


def test_initialize_store_migrates_legacy_signal_schema(tmp_path: Path) -> None:
    store = tmp_path / "signals.db"
    conn = sqlite3.connect(store)
    conn.executescript(
        """
        CREATE TABLE shadow_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE shadow_signals (
            source_key TEXT PRIMARY KEY, handler_version TEXT NOT NULL, board TEXT NOT NULL,
            board_db TEXT NOT NULL, board_fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
            run_id INTEGER NOT NULL, hook_name TEXT NOT NULL, outcome TEXT NOT NULL,
            retry_status TEXT NOT NULL, discovery_source TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, claimed_by TEXT, claim_expires INTEGER,
            recorded_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, processed_outcome TEXT,
            supervisor_comparison TEXT NOT NULL DEFAULT 'unknown', last_error_class TEXT
        );
        """
    )
    conn.close()

    shadow.initialize_store(store)

    conn = sqlite3.connect(store)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(shadow_signals)")}
    version = conn.execute(
        "SELECT value FROM shadow_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert {
        "source_event_id", "source_event_kind", "source_event_created_at",
        "canonical_source_key", "detected_at", "time_to_detection_seconds",
        "callback_recorded_at", "callback_observations",
        "callback_duplicate_suppressions", "audit_recorded_at",
        "audit_observations", "audit_duplicate_suppressions",
        "retry_not_before", "claim_token",
    } <= columns
    assert version == ("4",)
    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'shadow_signals_board_due'"
    ).fetchone()
    conn.close()
    assert index_sql is not None
    assert "retry_not_before" in index_sql[0]


def test_callback_signal_is_deduplicated_and_consumer_revalidates(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board)
    shadow.initialize_store(store)

    recorded = shadow.record_hook_signal(
        store_path=store, board="control", board_db=board,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=11,
    )
    duplicate = shadow.record_hook_signal(
        store_path=store, board="control", board_db=board,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=12,
    )

    assert recorded["recorded"] is True
    assert duplicate == {"source_key": recorded["source_key"], "recorded": False}
    assert shadow.consume_one(
        store_path=store, board_db=board, owner="test", now=13
    ) == {"source_key": recorded["source_key"], "outcome": "detected_current"}
    assert shadow.shadow_summary(store)["states"] == {"done": 1}


def test_consumer_discards_superseded_signal_without_task_mutation(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board, status="running", current_run_id=10)
    shadow.initialize_store(store)
    recorded = shadow.record_hook_signal(
        store_path=store, board="control", board_db=board,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=11,
    )

    result = shadow.consume_one(store_path=store, board_db=board, owner="test", now=12)

    assert result == {"source_key": recorded["source_key"], "outcome": "superseded"}
    conn = sqlite3.connect(board)
    assert conn.execute("SELECT status, current_run_id FROM tasks WHERE id = 't_event'").fetchone() == ("running", 10)
    conn.close()


def test_consumer_retries_unavailable_board_and_recovers_after_restore(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    unavailable_board = tmp_path / "board.unavailable"
    store = tmp_path / "signals.db"
    create_board(board)
    shadow.initialize_store(store)
    assert shadow.reconcile_events(
        store_path=store, board="control", board_db=board, now=11
    ) == {"scanned": 1, "recorded": 1, "duplicates": 0, "excluded": 0}

    board.rename(unavailable_board)
    pending = shadow.consume_one(store_path=store, board_db=board, owner="test", now=12)

    assert pending is not None
    assert pending["outcome"] == "board_unavailable"
    summary = shadow.shadow_summary(store)
    assert summary["states"] == {"pending": 1}
    assert summary["outcomes"] == {"retry_scheduled": 1}
    assert summary["eligible_event_evidence"][0]["recovery"] == {
        "attempts": 1,
        "last_error_class": "board_unavailable",
        "retry_not_before": 42,
    }
    assert shadow.consume_one(store_path=store, board_db=board, owner="early", now=41) is None

    unavailable_board.rename(board)
    recovered = shadow.consume_one(store_path=store, board_db=board, owner="retry", now=42)

    assert recovered is not None
    assert recovered["outcome"] == "detected_current"
    assert shadow.shadow_summary(store)["states"] == {"done": 1}
    assert shadow.shadow_summary(store)["eligible_event_evidence"][0]["recovery"] == {
        "attempts": 2,
        "last_error_class": None,
        "retry_not_before": None,
    }


def test_consumer_retries_board_schema_error_and_recovers_after_restore(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    original_board = tmp_path / "board.original"
    store = tmp_path / "signals.db"
    create_board(board)
    shadow.initialize_store(store)
    assert shadow.reconcile_events(
        store_path=store, board="control", board_db=board, now=11
    ) == {"scanned": 1, "recorded": 1, "duplicates": 0, "excluded": 0}

    board.rename(original_board)
    sqlite3.connect(board).close()
    pending = shadow.consume_one(store_path=store, board_db=board, owner="test", now=12)

    assert pending is not None
    assert pending["outcome"] == "board_schema_error"
    assert shadow.shadow_summary(store)["eligible_event_evidence"][0]["recovery"] == {
        "attempts": 1,
        "last_error_class": "board_schema_error",
        "retry_not_before": 42,
    }

    board.unlink()
    original_board.rename(board)
    recovered = shadow.consume_one(store_path=store, board_db=board, owner="retry", now=42)

    assert recovered is not None
    assert recovered["outcome"] == "detected_current"


def test_stale_same_owner_lease_cannot_finish_or_release_newer_claim(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board)
    shadow.initialize_store(store)
    shadow.record_hook_signal(
        store_path=store, board="control", board_db=board,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=10,
    )
    fingerprint = shadow.board_fingerprint(board)
    first = shadow._claim_next(store, fingerprint, "shared-owner", "first", 11, 1)
    second = shadow._claim_next(store, fingerprint, "shared-owner", "second", 13, 60)

    assert first is not None
    assert second is not None
    assert first["claim_token"] == "first"
    assert second["claim_token"] == "second"
    assert shadow._finish_signal(store, first, "shared-owner", "detected_current", 14) is False
    assert shadow._release_for_retry(store, first, "shared-owner", "board_unavailable", 14) is False
    conn = sqlite3.connect(store)
    assert conn.execute(
        "SELECT state, claimed_by, claim_token, attempts FROM shadow_signals"
    ).fetchone() == ("claimed", "shared-owner", "second", 2)
    conn.close()
    assert shadow._finish_signal(store, second, "shared-owner", "detected_current", 14) is True


def test_reconciliation_recovers_a_missed_stale_claim_callback(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board, status="review", retry_status="review")
    conn = sqlite3.connect(board)
    conn.execute("UPDATE task_runs SET outcome = 'reclaimed' WHERE id = 9")
    conn.execute("UPDATE task_events SET kind = 'reclaimed' WHERE id = 21")
    conn.commit()
    conn.close()
    shadow.initialize_store(store)

    report = shadow.reconcile_events(store_path=store, board="control", board_db=board, now=11)

    assert report == {"scanned": 1, "recorded": 1, "duplicates": 0, "excluded": 0}
    conn = sqlite3.connect(store)
    assert conn.execute(
        "SELECT source_event_id, source_event_kind, source_event_created_at "
        "FROM shadow_signals"
    ).fetchone() == (21, "reclaimed", 10)
    conn.close()
    assert shadow.consume_one(store_path=store, board_db=board, owner="audit", now=12)["outcome"] == "detected_current"


def test_reconciliation_excludes_manual_reclaims_but_advances_cursor(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board, status="review", retry_status="review")
    conn = sqlite3.connect(board)
    conn.execute("UPDATE task_runs SET outcome = 'reclaimed' WHERE id = 9")
    conn.execute(
        "UPDATE task_events SET kind = 'reclaimed', payload = ? WHERE id = 21",
        (json.dumps({"manual": True, "retry_status": "review"}),),
    )
    conn.commit()
    conn.close()
    shadow.initialize_store(store)

    assert shadow.reconcile_events(
        store_path=store, board="control", board_db=board, now=11
    ) == {"scanned": 0, "recorded": 0, "duplicates": 0, "excluded": 1}
    assert shadow.shadow_summary(store)["eligible_events"] == 0
    assert shadow.reconcile_events(
        store_path=store, board="control", board_db=board, now=12
    ) == {"scanned": 0, "recorded": 0, "duplicates": 0, "excluded": 0}


def test_reconciliation_uses_source_retry_status_after_successor_and_continues(
    tmp_path: Path,
) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board, status="running", current_run_id=10, retry_status="ready")
    conn = sqlite3.connect(board)
    conn.execute("INSERT INTO tasks VALUES ('t_later', 'ready', NULL)")
    conn.execute("INSERT INTO task_runs VALUES (11, 't_later', 'crashed', 12)")
    conn.execute(
        "INSERT INTO task_events VALUES (22, 't_later', 11, 'crashed', 12, ?)",
        (json.dumps({"retry_status": "ready"}),),
    )
    conn.commit()
    conn.close()
    shadow.initialize_store(store)

    assert shadow.reconcile_events(
        store_path=store, board="control", board_db=board, now=20
    ) == {"scanned": 2, "recorded": 2, "duplicates": 0, "excluded": 0}
    conn = sqlite3.connect(store)
    retry_statuses = dict(conn.execute("SELECT task_id, retry_status FROM shadow_signals"))
    conn.close()
    assert retry_statuses == {"t_event": "ready", "t_later": "ready"}
    outcomes = {
        shadow.consume_one(store_path=store, board_db=board, owner="consumer", now=21)["outcome"],
        shadow.consume_one(store_path=store, board_db=board, owner="consumer", now=22)["outcome"],
    }
    assert outcomes == {"superseded", "detected_current"}


def test_consumer_claims_only_the_requested_board(tmp_path: Path) -> None:
    board_a = tmp_path / "board-a.db"
    board_b = tmp_path / "board-b.db"
    store = tmp_path / "signals.db"
    create_board(board_a)
    create_board(board_b)
    shadow.initialize_store(store)
    first = shadow.record_hook_signal(
        store_path=store, board="a", board_db=board_a,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=11,
    )
    second = shadow.record_hook_signal(
        store_path=store, board="b", board_db=board_b,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=12,
    )

    assert shadow.consume_one(
        store_path=store, board_db=board_b, owner="consumer-b", now=13
    ) == {"source_key": second["source_key"], "outcome": "detected_current"}
    conn = sqlite3.connect(store)
    states = dict(conn.execute("SELECT source_key, state FROM shadow_signals"))
    conn.close()
    assert states[first["source_key"]] == "pending"
    assert states[second["source_key"]] == "done"


def test_callback_and_audit_converge_on_one_durable_event_identity(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board)
    shadow.initialize_store(store)
    callback = shadow.record_hook_signal(
        store_path=store, board="control", board_db=board,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=20,
    )
    duplicate = shadow.record_hook_signal(
        store_path=store, board="control", board_db=board,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=21,
    )
    assert duplicate == {"source_key": callback["source_key"], "recorded": False}

    assert shadow.reconcile_events(
        store_path=store, board="control", board_db=board, now=25
    ) == {"scanned": 1, "recorded": 0, "duplicates": 1, "excluded": 0}
    assert shadow.consume_one(
        store_path=store, board_db=board, owner="consumer", now=30
    ) == {"source_key": callback["source_key"], "outcome": "detected_current"}
    conn = sqlite3.connect(store)
    row = conn.execute(
        "SELECT source_event_id, source_event_kind, source_event_created_at, "
        "canonical_source_key, time_to_detection_seconds FROM shadow_signals"
    ).fetchone()
    conn.close()
    assert row == (21, "crashed", 10, row[3], 20)
    assert row[3]
    summary = shadow.shadow_summary(store)
    assert summary["eligible_events"] == 1
    assert summary["time_to_detection"] == {
        "count": 1, "min_seconds": 20, "max_seconds": 20, "total_seconds": 20
    }
    assert summary["observation_evidence"] == {
        "callback": {"events_recorded": 1, "observations": 2, "duplicate_suppressions": 1},
        "audit": {"events_recorded": 0, "observations": 1, "duplicate_suppressions": 1},
    }
    assert summary["eligible_event_evidence"] == [{
        "board_fingerprint": shadow.board_fingerprint(board),
        "source_event_id": 21,
        "task_id": "t_event",
        "run_id": 9,
        "source_event_kind": "crashed",
        "state": "done",
        "recovery": {
            "attempts": 1,
            "last_error_class": None,
            "retry_not_before": None,
        },
        "callback": {"recorded_at": 20, "observations": 2, "duplicate_suppressions": 1},
        "audit": {"recorded_at": None, "observations": 1, "duplicate_suppressions": 1},
    }]


def test_comparison_metrics_track_missed_and_false_positive_cases(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board)
    shadow.initialize_store(store)
    first = shadow.record_hook_signal(
        store_path=store, board="control", board_db=board,
        hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
        retry_status="ready", outcome="crashed", now=11,
    )
    assert shadow.record_comparison(store, first["source_key"], "missed") is True
    assert shadow.shadow_summary(store)["missed_cases"] == 1
    assert shadow.record_comparison(store, first["source_key"], "false_positive") is True
    assert shadow.shadow_summary(store)["false_positives"] == 1


def test_callback_rejects_unexpected_payload_before_store_write(tmp_path: Path) -> None:
    store = tmp_path / "signals.db"
    shadow.initialize_store(store)
    try:
        shadow.record_hook_signal(
            store_path=store, board="control", board_db=tmp_path / "board.db",
            hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
            retry_status="ready", outcome="completed",
        )
    except ValueError as exc:
        assert str(exc) == "invalid_exit_outcome"
    else:
        raise AssertionError("unexpected exit outcomes must fail closed")
    assert shadow.shadow_summary(store)["signals"] == 0


def test_runtime_rejects_relative_store_and_board_paths(tmp_path: Path) -> None:
    store = tmp_path / "signals.db"
    original_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        try:
            shadow.initialize_store("relative-signals.db")
        except ValueError as exc:
            assert str(exc) == "store_path_must_be_absolute_normalized"
        else:
            raise AssertionError("relative signal store paths must fail closed")
        assert not (tmp_path / "relative-signals.db").exists()
    finally:
        os.chdir(original_cwd)

    for invalid_store, error in (
        (f"{tmp_path}/./non-normalized-signals.db", "store_path_must_be_absolute_normalized"),
        (tmp_path, "store_path_must_be_regular_file"),
    ):
        try:
            shadow.initialize_store(invalid_store)
        except ValueError as exc:
            assert str(exc) == error
        else:
            raise AssertionError("non-private signal store paths must fail closed")
    assert not (tmp_path / "non-normalized-signals.db").exists()

    dangling_target = tmp_path / "missing-store-target.db"
    dangling_store = tmp_path / "dangling-store.db"
    dangling_store.symlink_to(dangling_target)
    symlinked_parent_target = tmp_path / "store-state"
    symlinked_parent_target.mkdir()
    symlinked_parent = tmp_path / "symlinked-state"
    symlinked_parent.symlink_to(symlinked_parent_target, target_is_directory=True)
    for invalid_store in (dangling_store, symlinked_parent / "signals.db"):
        try:
            shadow.initialize_store(invalid_store)
        except ValueError as exc:
            assert str(exc) == "store_path_must_be_regular_file"
        else:
            raise AssertionError("symlinked signal store paths must fail closed")
    assert not dangling_target.exists()
    assert not (symlinked_parent_target / "signals.db").exists()

    try:
        shadow.initialize_store("/tmp/kanban-event-shadow-untrusted.db")
    except ValueError as exc:
        assert str(exc) == "store_parent_must_be_private"
    else:
        raise AssertionError("a store without a private ancestor must fail closed")

    shadow.initialize_store(store)
    try:
        shadow.record_hook_signal(
            store_path=store, board="control", board_db="relative-board.db",
            hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
            retry_status="ready", outcome="crashed",
        )
    except ValueError as exc:
        assert str(exc) == "board_db_must_be_absolute_normalized"
    else:
        raise AssertionError("relative canonical board paths must fail closed")
    try:
        shadow.record_hook_signal(
            store_path=store, board="control", board_db=tmp_path / "missing-board.db",
            hook_name=shadow.HOOK_EXITED, task_id="t_event", run_id=9,
            retry_status="ready", outcome="crashed",
        )
    except ValueError as exc:
        assert str(exc) == "board_db_must_be_regular_file"
    else:
        raise AssertionError("missing canonical board paths must fail closed")


def test_store_open_rejects_a_path_swap_before_sqlite_use(tmp_path: Path) -> None:
    store = tmp_path / "signals.db"
    replacement = tmp_path / "replacement.db"
    original_connect = shadow.sqlite3.connect

    class Connection:
        def close(self) -> None:
            return None

    def swap_path(*args: object, **kwargs: object) -> Connection:
        store.unlink()
        store.symlink_to(replacement)
        return Connection()

    shadow.sqlite3.connect = swap_path
    try:
        try:
            shadow.initialize_store(store)
        except ValueError as exc:
            assert str(exc) == "store_path_changed_during_open"
        else:
            raise AssertionError("a store pathname swap must fail closed")
    finally:
        shadow.sqlite3.connect = original_connect
    assert store.is_symlink()
    assert not replacement.exists()


def test_cli_rejects_invalid_board_before_store_initialization(tmp_path: Path) -> None:
    store = tmp_path / "signals.db"
    original_argv = sys.argv
    sys.argv = [
        "kanban_event_shadow.py", "consume", "--store", str(store),
        "--board-db", "relative-board.db", "--owner", "test",
    ]
    try:
        try:
            shadow.main()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("invalid CLI board path must fail closed")
    finally:
        sys.argv = original_argv
    assert not store.exists()

    normalized_board = tmp_path / "board.db"
    create_board(normalized_board)
    non_normalized_store = tmp_path / "non-normalized-store.db"
    sys.argv = [
        "kanban_event_shadow.py", "consume", "--store", str(non_normalized_store),
        "--board-db", f"{tmp_path}/./board.db", "--owner", "test",
    ]
    try:
        try:
            shadow.main()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("non-normalized CLI board path must fail closed")
    finally:
        sys.argv = original_argv
    assert not non_normalized_store.exists()

    lexical_store = f"{tmp_path}/./lexical-store.db"
    sys.argv = ["kanban_event_shadow.py", "summary", "--store", lexical_store]
    try:
        try:
            shadow.main()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("non-normalized CLI store path must fail closed")
    finally:
        sys.argv = original_argv
    assert not (tmp_path / "lexical-store.db").exists()


def test_opt_in_plugin_rejects_non_absolute_configuration_without_store_write(
    tmp_path: Path,
) -> None:
    board = tmp_path / "board.db"
    create_board(board)
    plugin_path = (
        Path(__file__).resolve().parents[1]
        / "__init__.py"
    )
    plugin_spec = importlib.util.spec_from_file_location("invalid_shadow_plugin", plugin_path)
    assert plugin_spec is not None and plugin_spec.loader is not None
    plugin = importlib.util.module_from_spec(plugin_spec)
    previous_store = os.environ.get("HERMES_EVENT_SHADOW_STORE")
    previous_boards = os.environ.get("HERMES_EVENT_SHADOW_BOARD_DATABASES")
    original_cwd = Path.cwd()

    class Context:
        hooks: dict[str, object]

        def __init__(self) -> None:
            self.hooks = {}

        def register_hook(self, name: str, callback: object) -> None:
            self.hooks[name] = callback

    os.chdir(tmp_path)
    try:
        os.environ["HERMES_EVENT_SHADOW_STORE"] = "relative-signals.db"
        os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = json.dumps({"control": str(board)})
        plugin_spec.loader.exec_module(plugin)
        relative_store_context = Context()
        plugin.register(relative_store_context)
        assert relative_store_context.hooks == {}
        assert not (tmp_path / "relative-signals.db").exists()

        absolute_store = tmp_path / "signals.db"
        os.environ["HERMES_EVENT_SHADOW_STORE"] = str(absolute_store)
        os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = json.dumps(
            {"control": "relative-board.db"}
        )
        relative_board_context = Context()
        plugin.register(relative_board_context)
        assert relative_board_context.hooks == {}
        assert not absolute_store.exists()

        dangling_board_target = tmp_path / "missing-board-target.db"
        dangling_board = tmp_path / "dangling-board.db"
        dangling_board.symlink_to(dangling_board_target)
        symlink_board_store = tmp_path / "symlink-board-signals.db"
        os.environ["HERMES_EVENT_SHADOW_STORE"] = str(symlink_board_store)
        os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = json.dumps(
            {"control": str(dangling_board)}
        )
        symlink_board_context = Context()
        plugin.register(symlink_board_context)
        assert symlink_board_context.hooks == {}
        assert not symlink_board_store.exists()

        missing_board_store = tmp_path / "missing-board-signals.db"
        os.environ["HERMES_EVENT_SHADOW_STORE"] = str(missing_board_store)
        os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = json.dumps(
            {"control": str(tmp_path / "missing-board.db")}
        )
        missing_board_context = Context()
        plugin.register(missing_board_context)
        assert missing_board_context.hooks == {}
        assert not missing_board_store.exists()

        corrupt_store = tmp_path / "corrupt-signals.db"
        corrupt_store.write_text("not a SQLite database", encoding="utf-8")
        os.environ["HERMES_EVENT_SHADOW_STORE"] = str(corrupt_store)
        os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = json.dumps(
            {"control": str(board)}
        )
        corrupt_store_context = Context()
        plugin.register(corrupt_store_context)
        assert corrupt_store_context.hooks == {}
    finally:
        os.chdir(original_cwd)
        if previous_store is None:
            os.environ.pop("HERMES_EVENT_SHADOW_STORE", None)
        else:
            os.environ["HERMES_EVENT_SHADOW_STORE"] = previous_store
        if previous_boards is None:
            os.environ.pop("HERMES_EVENT_SHADOW_BOARD_DATABASES", None)
        else:
            os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = previous_boards


def test_opt_in_plugin_records_only_allow_listed_board(tmp_path: Path) -> None:
    board = tmp_path / "board.db"
    store = tmp_path / "signals.db"
    create_board(board)
    plugin_path = (
        Path(__file__).resolve().parents[1]
        / "__init__.py"
    )
    plugin_spec = importlib.util.spec_from_file_location("test_shadow_plugin", plugin_path)
    assert plugin_spec is not None and plugin_spec.loader is not None
    plugin = importlib.util.module_from_spec(plugin_spec)
    previous_store = os.environ.get("HERMES_EVENT_SHADOW_STORE")
    previous_boards = os.environ.get("HERMES_EVENT_SHADOW_BOARD_DATABASES")
    os.environ["HERMES_EVENT_SHADOW_STORE"] = str(store)
    os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = '{"control": "' + str(board) + '"}'
    try:
        plugin_spec.loader.exec_module(plugin)

        class Context:
            hooks: dict[str, object] = {}

            def register_hook(self, name: str, callback: object) -> None:
                self.hooks[name] = callback

        context = Context()
        plugin.register(context)
        callback = context.hooks[shadow.HOOK_EXITED]
        callback(  # type: ignore[operator]
            board="control", task_id="t_event", run_id=9,
            retry_status="ready", outcome="crashed",
        )
        assert shadow.shadow_summary(store)["signals"] == 1
    finally:
        if previous_store is None:
            os.environ.pop("HERMES_EVENT_SHADOW_STORE", None)
        else:
            os.environ["HERMES_EVENT_SHADOW_STORE"] = previous_store
        if previous_boards is None:
            os.environ.pop("HERMES_EVENT_SHADOW_BOARD_DATABASES", None)
        else:
            os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = previous_boards


if __name__ == "__main__":
    tests = [
        test_initialize_store_migrates_legacy_signal_schema,
        test_callback_signal_is_deduplicated_and_consumer_revalidates,
        test_consumer_discards_superseded_signal_without_task_mutation,
        test_consumer_retries_unavailable_board_and_recovers_after_restore,
        test_consumer_retries_board_schema_error_and_recovers_after_restore,
        test_stale_same_owner_lease_cannot_finish_or_release_newer_claim,
        test_reconciliation_recovers_a_missed_stale_claim_callback,
        test_reconciliation_excludes_manual_reclaims_but_advances_cursor,
        test_reconciliation_uses_source_retry_status_after_successor_and_continues,
        test_consumer_claims_only_the_requested_board,
        test_callback_and_audit_converge_on_one_durable_event_identity,
        test_comparison_metrics_track_missed_and_false_positive_cases,
        test_callback_rejects_unexpected_payload_before_store_write,
        test_runtime_rejects_relative_store_and_board_paths,
        test_store_open_rejects_a_path_swap_before_sqlite_use,
        test_cli_rejects_invalid_board_before_store_initialization,
        test_opt_in_plugin_rejects_non_absolute_configuration_without_store_write,
        test_opt_in_plugin_records_only_allow_listed_board,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
    print(f"{len(tests)} tests passed")
