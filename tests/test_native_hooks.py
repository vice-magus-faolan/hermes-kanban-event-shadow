"""Pinned-native loader and lifecycle coverage on disposable canonical boards.

Native stale-claim transactions are real; fixture inputs and lease expiry are
synthetic. No worker is spawned and no live board or profile is accessed.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = Path(os.environ["HERMES_AGENT_ROOT"]).resolve()
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))


class NativeShadowHookIntegrationTests(unittest.TestCase):
    """Exercise package registration and native Kanban observer call sites."""

    def setUp(self) -> None:
        scratch_root = Path(os.environ["TMPDIR"])
        scratch_root.mkdir(parents=True, exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(dir=scratch_root)
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.root.chmod(0o700)
        self.home = self.root / "hermes-home"
        self.home.mkdir(mode=0o700)
        self.board_dir = self.root / "boards"
        self.board_dir.mkdir(mode=0o700)
        self.board_path = self.board_dir / "kanban.db"
        self.store = self.root / "shadow" / "signals.db"
        (self.root / "shadow").mkdir(mode=0o700)
        self.board_slug = "shadow-native-fixture"

        self.constants = importlib.import_module("hermes_constants")
        self.plugins = importlib.import_module("hermes_cli.plugins")
        self.lifecycle = importlib.import_module("hermes_cli.lifecycle")
        self.kb = importlib.import_module("hermes_cli.kanban_db")
        self.kb_connect = importlib.import_module("hermes_cli.kanban_db_connect")
        self.dispatch = importlib.import_module("hermes_cli.kanban_db_dispatch")
        self.manifest_api = importlib.import_module("hermes_cli.plugins_manifest")

        self._home_token = self.constants.set_hermes_home_override(self.home)
        self.addCleanup(self.constants.reset_hermes_home_override, self._home_token)
        env_patch = mock.patch.dict(
            os.environ,
            {
                "TMPDIR": str(scratch_root),
                "HERMES_HOME": str(self.home),
                "HERMES_DEFAULT_HOME": str(self.home),
                "HERMES_KANBAN_HOME": str(self.home),
                "HERMES_KANBAN_BOARD": self.board_slug,
                "HERMES_EVENT_SHADOW_STORE": str(self.store),
                "HERMES_EVENT_SHADOW_BOARD_DATABASES": json.dumps(
                    {self.board_slug: str(self.board_path)}
                ),
            },
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        # The real fire site resolves get_current_board(), which requires a
        # registered native board; a database at an arbitrary path is not one.
        self.board_path = self.kb.kanban_db_path(board=self.board_slug)
        self.assertTrue(self.board_path.is_relative_to(self.home))
        self.assertTrue(self.kb.board_dir(self.board_slug).is_relative_to(self.home))
        self.kb.create_board(self.board_slug, name="disposable observer board")
        os.environ["HERMES_EVENT_SHADOW_BOARD_DATABASES"] = json.dumps({self.board_slug: str(self.board_path)})
        self.assertEqual(self.kb.get_current_board(), self.board_slug)
        native_conn = self.kb_connect.connect(db_path=self.board_path, board=self.board_slug)
        native_conn.close()
        self.manager = self.plugins.PluginManager(scope_key=str(self.home))
        plugin_manifest = self.manifest_api.parse_manifest_file(
            PACKAGE_ROOT / "plugin.yaml", PACKAGE_ROOT, "project", ""
        )
        self.manager._load_plugin(plugin_manifest)
        loaded = self.manager._plugins.get("kanban_event_shadow")
        self.assertIsNotNone(loaded, "native PluginManager should register the package manifest")
        self.assertTrue(loaded.enabled, getattr(loaded, "error", None))
        self.assertEqual(
            set(self.manager._hooks),
            {"on_kanban_worker_exited", "on_kanban_worker_stale_claim"},
        )
        self.wrapper_globals = self.manager._hooks["on_kanban_worker_exited"][0].__globals__
        self.runtime = self.wrapper_globals["RUNTIME"]
        self._install_scoped_delivery_manager()
        self.addCleanup(self._restore_delivery_manager)

    def _install_scoped_delivery_manager(self) -> None:
        """Make lifecycle.py's ordinary dispatcher resolve this isolated manager."""
        self._saved_manager = self.plugins._plugin_manager
        self._saved_managers_by_home = dict(self.plugins._plugin_managers_by_home)
        key = self.home.expanduser().resolve()
        self.plugins._plugin_managers_by_home[key] = self.manager
        setattr(self.plugins, "_plugin_manager", self.manager)
        self.manager._discovered = True

    def _restore_delivery_manager(self) -> None:
        setattr(self.plugins, "_plugin_manager", self._saved_manager)
        self.plugins._plugin_managers_by_home.clear()
        self.plugins._plugin_managers_by_home.update(self._saved_managers_by_home)

    def _create_claimed_fixture_task(self) -> str:
        conn = self.kb_connect.connect(db_path=self.board_path, board=self.board_slug)
        try:
            task_id = self.kb.create_task(
                conn,
                title="disposable shadow observer fixture",
                body="synthetic canonical fixture; no worker is spawned",
                created_by="unittest",
                board=self.board_slug,
            )
            self.assertTrue(self.kb.claim_task(conn, task_id, claimer="unittest-native"))
            conn.execute(
                "UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task_id,)
            )
            conn.commit()
            return task_id
        finally:
            conn.close()

    def _canonical_event_and_run(self, task_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        conn = sqlite3.connect(self.board_path)
        conn.row_factory = sqlite3.Row
        try:
            event = conn.execute(
                "SELECT id, task_id, run_id, kind, payload FROM task_events "
                "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            run = conn.execute(
                "SELECT id, task_id, outcome, ended_at FROM task_runs "
                "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            self.assertIsNotNone(event)
            self.assertIsNotNone(run)
            return event, run
        finally:
            conn.close()

    def test_unconfigured_registration_is_inert_and_creates_no_store(self) -> None:
        """No opt-in means no hooks and no sidecar database."""
        self._restore_delivery_manager()
        import shutil
        shutil.rmtree(self.store.parent, ignore_errors=True)
        with mock.patch.dict(
            os.environ,
            {"HERMES_EVENT_SHADOW_STORE": "", "HERMES_EVENT_SHADOW_BOARD_DATABASES": ""},
        ):
            inert = self.plugins.PluginManager(scope_key=str(self.home / "inert"))
            manifest = self.manifest_api.parse_manifest_file(
                PACKAGE_ROOT / "plugin.yaml", PACKAGE_ROOT, "project", ""
            )
            inert._load_plugin(manifest)
            loaded = inert._plugins.get("kanban_event_shadow")
            self.assertIsNotNone(loaded)
            self.assertTrue(loaded.enabled)
            self.assertEqual(inert._hooks, {})
            self.assertFalse(self.store.exists())

    def test_native_plugin_manager_dispatch_is_board_allowlisted_and_best_effort(self) -> None:
        """Native loader/dispatch routes callbacks; unknown boards are ignored."""
        self.assertEqual(self.manager.invoke_hook(
            "on_kanban_worker_exited", board="unlisted-board", task_id="opaque", run_id=7,
            retry_status="ready", outcome="crashed",
        ), [])
        self.assertEqual(self.runtime.shadow_summary(self.store)["signals"], 0)
        self.assertEqual(self.manager.invoke_hook(
            "on_kanban_worker_exited", board=self.board_slug, task_id="fixture",
            run_id=7, retry_status="ready", outcome="crashed",
        ), [])
        runtime = self.runtime
        self.assertEqual(runtime.shadow_summary(self.store)["signals"], 1)
        with mock.patch.dict(self.wrapper_globals, {"record_hook_signal": mock.Mock(side_effect=OSError("fixture I/O fault"))}):
            self.assertEqual(self.manager.invoke_hook(
                "on_kanban_worker_exited", board=self.board_slug, task_id="fixture-2",
                run_id=8, retry_status="ready", outcome="crashed",
            ), [])
        self.assertEqual(runtime.shadow_summary(self.store)["signals"], 1)

    def test_native_stale_claim_lifecycle_site_observes_committed_reclaim(self) -> None:
        """Real release_stale_claims calls lifecycle dispatch after native commit."""
        task_id = self._create_claimed_fixture_task()
        observed_after_commit = []
        record_signal = self.wrapper_globals["record_hook_signal"]

        def observe_and_record(**kwargs):
            event, run = self._canonical_event_and_run(kwargs["task_id"])
            self.assertEqual(event["kind"], "reclaimed")
            self.assertEqual(run["outcome"], "reclaimed")
            self.assertIsNotNone(run["ended_at"])
            observed_after_commit.append(event["id"])
            return record_signal(**kwargs)

        self.assertTrue(self.plugins.has_hook("on_kanban_worker_stale_claim"))
        conn = self.kb_connect.connect(db_path=self.board_path, board=self.board_slug)
        try:
            with mock.patch.dict(self.wrapper_globals, {"record_hook_signal": observe_and_record}):
                self.assertEqual(self.kb.release_stale_claims(conn), 1)
        finally:
            conn.close()

        self.assertEqual(len(observed_after_commit), 1)
        event, run = self._canonical_event_and_run(task_id)
        self.assertEqual(event["kind"], "reclaimed")
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(event["run_id"], run["id"])
        payload = json.loads(event["payload"])
        self.assertFalse(payload.get("manual", False))
        runtime = self.runtime
        summary = runtime.shadow_summary(self.store)
        self.assertEqual(summary["signals"], 1)
        signal = sqlite3.connect(self.store)
        try:
            row = signal.execute(
                "SELECT hook_name, task_id, run_id, outcome, retry_status FROM shadow_signals"
            ).fetchone()
        finally:
            signal.close()
        self.assertEqual(row[0], runtime.HOOK_STALE)
        self.assertEqual(row[1], task_id)
        self.assertEqual(row[2], run["id"])
        self.assertEqual(row[3], "reclaimed")
        self.assertIn(row[4], {"ready", "review"})
        canonical_bytes = self.board_path.read_bytes()
        result = runtime.consume_one(store_path=self.store, board_db=self.board_path, owner="native-fixture-observer")
        self.assertEqual(result["outcome"], "detected_current")
        self.assertEqual(runtime.shadow_summary(self.store)["eligible_events"], 1)
        self.assertEqual(self.board_path.read_bytes(), canonical_bytes)

    def test_native_worker_exit_site_records_committed_crash_from_synthetic_process_view(self) -> None:
        """Real detect_crashed_workers produces the event; no OS worker is spawned."""
        task_id = self._create_claimed_fixture_task()
        conn = self.kb_connect.connect(db_path=self.board_path, board=self.board_slug)
        try:
            # Fixture-only process metadata. Liveness and reap status below are
            # explicitly synthetic, not proof of an actual worker's exit.
            conn.execute(
                "UPDATE tasks SET claim_lock = ?, worker_pid = 7, started_at = 1 WHERE id = ?",
                (self.kb._host_prefix() + "synthetic-observer", task_id),
            )
            conn.commit()
            observed_after_commit = []
            record_signal = self.wrapper_globals["record_hook_signal"]

            def observe_and_record(**kwargs):
                event, run = self._canonical_event_and_run(kwargs["task_id"])
                self.assertEqual(event["kind"], "crashed")
                self.assertEqual(run["outcome"], "crashed")
                self.assertIsNotNone(run["ended_at"])
                observed_after_commit.append(event["id"])
                return record_signal(**kwargs)

            with mock.patch.object(self.dispatch, "_worker_alive", return_value=False):
                with mock.patch.object(self.dispatch, "_classify_worker_exit", return_value=("nonzero_exit", 1)):
                    with mock.patch.dict(self.wrapper_globals, {"record_hook_signal": observe_and_record}):
                        self.assertEqual(self.dispatch.detect_crashed_workers(conn, board=self.board_slug), [task_id])
        finally:
            conn.close()
        self.assertEqual(len(observed_after_commit), 1)
        event, run = self._canonical_event_and_run(task_id)
        self.assertEqual(event["run_id"], run["id"])
        self.assertEqual(self.runtime.shadow_summary(self.store)["signals"], 1)
        canonical_bytes = self.board_path.read_bytes()
        result = self.runtime.consume_one(store_path=self.store, board_db=self.board_path, owner="native-fixture-observer")
        self.assertEqual(result["outcome"], "detected_current")
        self.assertEqual(self.runtime.shadow_summary(self.store)["eligible_events"], 1)
        self.assertEqual(self.board_path.read_bytes(), canonical_bytes)

    def test_native_stale_claim_callback_failure_does_not_abort_reclaim(self) -> None:
        """The native lifecycle site remains successful if shadow persistence fails."""
        task_id = self._create_claimed_fixture_task()
        runtime = self.runtime
        failed_record = mock.Mock(side_effect=OSError("fixture I/O fault"))
        with mock.patch.dict(self.wrapper_globals, {"record_hook_signal": failed_record}):
            conn = self.kb_connect.connect(db_path=self.board_path, board=self.board_slug)
            try:
                self.assertEqual(self.kb.release_stale_claims(conn), 1)
            finally:
                conn.close()
        failed_record.assert_called_once()
        self.assertEqual(runtime.shadow_summary(self.store)["signals"], 0)
        event, run = self._canonical_event_and_run(task_id)
        self.assertEqual(event["kind"], "reclaimed")
        self.assertEqual(run["outcome"], "reclaimed")


if __name__ == "__main__":
    unittest.main()
