# Security and operational boundaries

This is experimental local observation/audit tooling, licensed GPL-3.0-or-later.
It is not a scheduler, task state machine, delivery authority or notification
transport. Do not use a shadow signal or operator comparison label to approve
work, unlock a delivery gate or skip an independent canonical lifecycle check.

## Trust assumptions

- Operator-selected process environment configures the store and board allowlist.
  Native hook payloads select only a configured board slug, never a supplied path.
- Environment is process-wide. Multiple enabled profiles with reused board slugs
  require intentional, unambiguous process/allowlist ownership.
- The hook API and native database schema are those of the exact documented
  Hermes source pin. Native schema/API changes need renewed compatibility tests.
- The store directory hierarchy must have a pre-existing private boundary. Avoid
  shared/writable ancestor paths; use operator-owned `0700` directories outside Git.
- Store files are effective-user-owned regular files opened no-follow and secured
  to `0600`, with descriptor/path identity checks before SQLite use.
- Absolute normalized paths, regular files and rejection of symlink components
  reduce accidental routing and ordinary local-user pathname hazards. They do
  not provide a full adversarial filesystem sandbox or prevent a compromised
  same-user process from changing data.

## Effects and failure

Canonical board connections are read-only. Store initialization, migration,
callback recording, claims, comparisons and summaries can write the separate
private shadow database. SQLite journal sidecars remain private runtime state.
The engine never spawns workers, runs event commands, controls tasks, wakes agents
or sends messages.

Callbacks are best-effort local observers; expected storage/configuration errors
are logged rather than becoming task authority. The consumer revalidates the
canonical run/event generation, uses a fenced private-store lease and retry
backoff, and retains superseded/transient evidence. Missed callbacks need an
explicit reconciliation invocation. Reconciliation can scan historical events;
it is not an automatically scheduled, bounded incremental production service.

Duplicate observations and invalid configuration remain meaningful failure
signals. Shadow stores and summaries can contain task IDs, configured paths,
claim identities and lifecycle evidence: do not commit them or publish raw logs.
Manual comparison classifications do not independently prove supervisor parity.
Do not reduce other monitoring/recovery solely because shadow tests are green.

## Reporting

Open a sanitized issue for non-sensitive bugs. For credentials, private task
content or local topology, use the repository owner's private contact mechanism
if available rather than a public issue. Do not paste live stores, configuration,
policies or process/session identifiers into public reports.

Runtime hardening and behavioral changes should be separately reviewed from the
exact extraction. No supported native installation or live activation is implied
by a source-only test or Plugin Doctor result.
