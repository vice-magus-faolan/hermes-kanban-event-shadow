#!/usr/bin/env python3
"""Run every isolated test and native Plugin Doctor against pinned Hermes source."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HERMES_COMMIT = "f42f579cf8bac4918ac9599bece71618afadd846"
MINIMUM_TESTS = 32
# A clean-source verification must not create caches that poison its next run.
sys.dont_write_bytecode = True


def stage_runtime_bundle(directory: Path) -> Path:
    """Stage only manifest/runtime files, excluding checkout and scratch trees."""
    bundle = directory / "runtime-bundle"
    bundle.mkdir()
    for filename in ("plugin.yaml", "__init__.py", "shadow_runtime.py"):
        shutil.copy2(ROOT / filename, bundle / filename)
    return bundle


def validate_native_source(value: str) -> tuple[Path, str]:
    """Refuse wrong/dirty source, including ignored Python and bytecode files."""
    source = Path(value).resolve(strict=True)
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    if actual != HERMES_COMMIT:
        raise ValueError(f"Unsupported test source: {actual}; expected {HERMES_COMMIT}")
    changed = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"], text=True,
    ).strip()
    if changed:
        raise ValueError("Native test source has tracked or untracked changes; use a clean checkout.")
    ignored = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], text=True,
    )
    importable = [name for name in ignored.split("\0") if Path(name).suffix in {".py", ".pyc", ".pyo"}]
    if importable:
        raise ValueError("Native test source contains ignored Python/bytecode files; use a fresh source-only checkout.")
    return source, actual


def run_native_doctor(bundle: Path, directory: str) -> int:
    """Use the actual native Doctor library without install/update bootstrap."""
    command = (
        "import sys; sys.path=" + repr(sys.path) + "; "
        "from hermes_cli.plugins_cmd import cmd_plugin_doctor; "
        "cmd_plugin_doctor(" + repr(str(bundle)) + ", ci=True)"
    )
    argv = [sys.executable, "-B"]
    if sys.flags.optimize:
        argv.append("-" + "O" * sys.flags.optimize)
    # Preserve interpreter dependency paths and every security/delegation marker.
    return subprocess.run([*argv, "-c", command], cwd=directory, check=False).returncode


def main() -> int:
    """Refuse unsupported source, incomplete discovery, skips or failed Doctor."""
    source_value = os.environ.get("HERMES_AGENT_ROOT")
    if not source_value:
        print("Set HERMES_AGENT_ROOT to a checkout of the documented Hermes commit.", file=sys.stderr)
        return 2
    try:
        source, actual = validate_native_source(source_value)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Cannot validate native test source: {exc}", file=sys.stderr)
        return 2
    scratch = Path(os.environ.get("TMPDIR", ROOT / ".test-scratch")).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kanban-shadow-verify-", dir=scratch) as name:
        home = Path(name) / "home"
        home.mkdir(mode=0o700)
        env = {
            "HERMES_HOME": str(home), "HERMES_DEFAULT_HOME": str(home),
            "HERMES_AGENT_ROOT": str(source), "TMPDIR": name,
            "HERMES_EVENT_SHADOW_STORE": "", "HERMES_EVENT_SHADOW_BOARD_DATABASES": "",
        }
        with patch.dict(os.environ, env):
            sys.path.insert(0, str(source))
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
            count = suite.countTestCases()
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            if count < MINIMUM_TESTS or not result.wasSuccessful() or result.skipped:
                print(f"Verification refused: {count} tests, {len(result.skipped)} skips.")
                return 1
            # Doctor copies its target below TMPDIR. Copying ROOT itself would
            # recurse with repository-local scratch or vendor sibling core source.
            code = run_native_doctor(stage_runtime_bundle(Path(name)), name)
            if code:
                return code
    print(f"VERIFIED: {count} tests; zero skips; native plugin doctor passed; core={actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
