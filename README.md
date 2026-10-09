# Hermes Kanban Event Shadow

**Experimental, opt-in audit tooling. Not a task controller or notification service.**

Native plugin ID: `kanban_event_shadow`. Supported platform: Linux.
License: GPL-3.0-or-later.

This plugin records selected post-commit native Kanban worker-exit and stale-claim
callbacks into a private SQLite store. A separately invoked deterministic engine
can reconcile missed callbacks against the canonical event log, revalidate each
signal, and record detection/duplicate/recovery evidence. It never changes the
canonical task lifecycle, dispatches workers, wakes agents, sends messages or
executes event payloads.

## Why keep a shadow layer?

Native Hermes already supplies `task_events`, the two observer hooks and its
notification bridge. This is additional measurement, not a replacement for those
features: it tracks callback/audit convergence, canonical event identity,
lease-fenced observation, retry/backoff and detection latency. Supervisor-parity
classifications are explicitly entered by an operator; they are not discovered
or proven automatically.

No production-parity claim is made. Do not remove supervisor, maintenance or
notification responsibilities because a synthetic test suite passes.

## Package and scope

- `__init__.py`: original wrapper, with only package-local loader paths adapted.
- `plugin.yaml`: unchanged original manifest.
- `shadow_runtime.py`: byte-identical extracted engine and CLI; stdlib only.
- `tests/inherited_shadow.py`: all 18 original fixture functions, with only
  package-local file references adapted.
- `tests/test_inherited.py`: assertion-preserving unittest adapter.
- `tests/test_native_hooks.py`: genuine native loader and post-commit stale-claim
  and worker-exit transaction coverage on disposable boards. Worker liveness and
  reap status are explicitly synthetic; no OS worker is spawned.
- `scripts/verify.py`: isolated source-only verification and native Plugin Doctor.

See `docs/provenance.md` for exact source hashes and disclosed adaptations.
There is no workspace script import, service installer or automatic schedule.
The plugin has no model-facing tools or runtime Python package dependencies.

## Installation is a separate, explicit action

This extraction does **not** enable, replace or remove any live installation.
Its implementation is reviewed in a PR above a bootstrap-only `main` branch.
Wait for human review and merge before treating `main` as an installable artifact.

For an approved later repo-linked installation, keep one physical checkout at
`~/source/repos/hermes-kanban-event-shadow`. Each authorized profile plugin path
may point to that checkout; do not vendor another implementation into the profile.
For example, after selecting a reviewed artifact and the intended profile:

```bash
SOURCE="$HOME/source/repos/hermes-kanban-event-shadow"
PROFILE_HOME="$HOME/.hermes"  # explicitly choose the intended profile
mkdir -p "$PROFILE_HOME/plugins"
# Refuses if an existing installation has not been separately reconciled.
ln -s "$SOURCE" "$PROFILE_HOME/plugins/kanban_event_shadow"
hermes -p default plugins doctor "$SOURCE" --ci
```

A directory link is not PM-managed Git adoption. A link to a writable checkout
also couples future source advancement to runtime loading: keep feature writers
out of a live reviewed integration checkout. Installation, activation, environment
configuration, consumer scheduling and polling reduction require separate
operational decisions. No gateway restart is part of this extraction.

## Explicit configuration

Two process-environment variables opt the recorder into a specific private store
and explicit board allowlist:

```text
HERMES_EVENT_SHADOW_STORE=/absolute/private/directory/signals.db
HERMES_EVENT_SHADOW_BOARD_DATABASES={"example-board":"/absolute/canonical/board.db"}
```

These paths are placeholders, not runnable production configuration. Use actual
absolute, normalized paths without symlink components. Board entries must be
existing regular files. Pre-create a private directory boundary for the store,
such as an operator-owned `0700` directory; the store is opened as an effective-user
owned regular file with `0600` permissions. Keep it outside Git.

If either variable is missing/empty, the wrapper registers **zero hooks** and
creates no store. Malformed/invalid configuration logs a refusal and does not
register the observer callbacks. Unconfigured Plugin Doctor can therefore pass
with warnings that the manifest's declared hooks were not registered; that is
expected and does not prove configured callbacks work.

Environment configuration is process-wide, not a profile-local configuration
schema. Do not assume identically named boards in multiple profiles map to the
same canonical database. Enable only an intentionally scoped experiment, with an
unambiguous board-slug/path allowlist and process ownership. No model-supplied
board path is accepted by the callback wrapper.

## CLI and effects

Every CLI invocation initializes/migrates the private store. Even `summary` is
**not a read-only filesystem operation**. Board databases are read in SQLite
read-only mode; only the separate shadow store is written by this engine.

```bash
python3 -I shadow_runtime.py --help
python3 -I shadow_runtime.py reconcile --store /absolute/private/signals.db \
  --board example-board --board-db /absolute/canonical/board.db
python3 -I shadow_runtime.py consume --store /absolute/private/signals.db \
  --board-db /absolute/canonical/board.db --owner example-observer
python3 -I shadow_runtime.py summary --store /absolute/private/signals.db
python3 -I shadow_runtime.py record-comparison --store /absolute/private/signals.db \
  --source-key EXACT_RECORDED_KEY --comparison detected
```

`consume` handles at most one due observation. A superseded generation is not
reported as current; unavailable/schema-error boards retain retryable evidence
with backoff. `reconcile` can scan the eligible historical event set: it has no
incremental cursor or universal large-board replay bound. Summary evidence can
also grow with store history. Benchmark a representative disposable board before
choosing any production cadence or retention policy.

The original engine remains unchanged. Its path/privacy checks constrain ordinary
local use; they are not a cryptographic provenance boundary, an adversarial
same-user security sandbox or authorization to control Kanban work. See
`SECURITY.md` for trust and failure assumptions.

## Reproducible source-only verification

The tested native schema/API source is exactly:

```text
NousResearch/hermes-agent f42f579cf8bac4918ac9599bece71618afadd846
```

Use a fresh source-only checkout at that commit and a separate disposable test
venv. Test requirements are not plugin runtime dependencies and must never be
pip-installed into a live Hermes dependency generation.

```bash
python3 -m venv .venv-test
.venv-test/bin/python -I -m pip install -r requirements-test.txt
export HERMES_AGENT_ROOT=/absolute/fresh/pinned/hermes-agent
.venv-test/bin/python -I scripts/verify.py
.venv-test/bin/python -I -O scripts/verify.py
```

Verification rejects wrong/dirty/untracked source and ignored `.py`, `.pyc` or
`.pyo` files anywhere in the native checkout. Keep venvs outside that native
checkout. It suppresses new bytecode, refuses skipped/incomplete/failing tests,
uses owned disposable homes/boards/stores, and stages only the three runtime files
for the actual native Doctor handler. It does not invoke installation bootstrap,
provision providers, clear security markers or activate the live plugin.

Original plain-assert fixtures are compiled with `optimize=0` by their adapter so
their assertions remain meaningful under the optimized interpreter; the runtime
itself imports in the interpreter's actual mode. A deliberate wrong-result
regression proves the original assertions are active. Directly running
`tests/inherited_shadow.py` under `-O` would **not** provide equivalent evidence.

Native source-only loader/observer coverage is distinct from supported PM
installation, live enablement, representative performance or production parity.
Only the pinned Linux source is a tested compatibility claim.
