"""Run every retained fixture with assertions active, even under Python -O.

The original fixture is intentionally not rewritten into unittest or pytest.
Explicit optimize=0 compilation preserves its assertions while the extracted
runtime still imports under the interpreter's real optimization mode.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

FIXTURE = Path(__file__).with_name("inherited_shadow.py")
inherited = types.ModuleType("shadow_inherited_fixture")
inherited.__file__ = str(FIXTURE)
sys.modules[inherited.__name__] = inherited
exec(compile(FIXTURE.read_text(), str(FIXTURE), "exec", optimize=0), inherited.__dict__)


def fixture_case(function):
    """Wrap one unchanged original function in an owned disposable directory."""
    def invoke() -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as name:
            function(Path(name))
    invoke.__name__ = function.__name__
    return unittest.FunctionTestCase(invoke, description=function.__name__)


class AssertionPreservationTests(unittest.TestCase):
    def test_inherited_assertions_reject_wrong_runtime_result_even_under_optimization(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as name:
            with patch.object(inherited.shadow, "consume_one", return_value={"outcome": "wrong"}):
                with self.assertRaises(AssertionError):
                    inherited.test_callback_signal_is_deduplicated_and_consumer_revalidates(Path(name))


def load_tests(loader, tests, pattern):
    """Expose exactly the original 18 test identifiers to normal discovery."""
    functions = [value for name, value in vars(inherited).items() if name.startswith("test_") and callable(value)]
    if len(functions) != 18:
        raise ValueError("Inherited shadow fixture must retain all 18 test functions")
    tests.addTests(fixture_case(function) for function in functions)
    return tests
