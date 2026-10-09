# Extraction provenance

This repository is a standalone extraction of four committed files from a
private Hermes workspace. No private workspace Git history, configuration,
policy, stores, receipts or unrelated scripts were imported.

Exact private-workspace snapshot: `972daab4959425489c207ddd01a8cf42c62a9add`.
The SHA is provenance, not a claim that its private history is publicly reachable.

## Exact source file hashes

- `plugins/kanban_event_shadow/__init__.py`: `21c989f0e6c59ca7f512c18f9b3f8fd85a3331ffbdfa2aeb1266605809828915`
- `plugins/kanban_event_shadow/plugin.yaml`: `1c3a1f9fd8b80cff308cc3650bd487ed0df2f37a52df4e47aaf6e4b06b0668fc`
- `scripts/kanban_event_shadow.py`: `720d51fa0c7de15f4ddc4cc1d033ca8137bb2221f384127905d94bf5a2587135`
- `scripts/tests/test_kanban_event_shadow.py`: `71ada8bd1d3c4e284b03670397fd1ecae2b54e798612e95c021af245988d35bb`

## Runtime preservation and adaptations

- Original `scripts/kanban_event_shadow.py` becomes `shadow_runtime.py`, byte-for-byte unchanged.
- Original `plugins/kanban_event_shadow/plugin.yaml` becomes root `plugin.yaml`, byte-for-byte unchanged. Native ID remains `kanban_event_shadow`.
- Original wrapper becomes root `__init__.py`. Exactly two loader expressions change: repository-local ROOT uses `.parent` instead of workspace `.parents[2]`, and the engine is `ROOT / "shadow_runtime.py"` rather than `ROOT / "scripts" / "kanban_event_shadow.py"`. Everything else is byte-identical to the original wrapper after these substitutions.
- All 18 original test function identifiers and the `create_board` helper remain. The helper and 16 test function ASTs are unchanged, including every signature and assertion. Only the two opt-in plugin tests adapt their loader paths to root `__init__.py`; no assertions, fixture authority, expected outcome or function identifiers change. The top-level engine import path also becomes package-local.
- The new unittest adapter explicitly compiles that inherited fixture with `optimize=0` to retain its plain assertions while runtime imports still honor the interpreter's actual optimization mode. A deliberate wrong-result regression establishes assertion activity.

Standalone docs/license/CI, strict source-selection verifier and genuine native
fixture coverage are newly owned support files, not falsely attributed to the
original source. The verifier rejects ignored Python/bytecode additions as well
as tracked/untracked changes; this narrow assurance improvement does not change
plugin runtime behavior.

Compatibility source: `NousResearch/hermes-agent` at
`f42f579cf8bac4918ac9599bece71618afadd846`, Linux only.
Source-only fixture and Doctor results do not establish PM admission, live
activation, representative performance or production supervisor parity.

The original private implementation was disabled when extracted. Its live files,
profile configuration, polling, schedules and policies remain unchanged by this
extraction; retirement or installation is a later approved action.
