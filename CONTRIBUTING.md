# Contributing

License: GPL-3.0-or-later. Runtime engine and manifest are an exact extraction;
please keep behavioral changes separate from packaging/loader adaptations.

Use a focused feature branch and implementation PR. `main` is initially bootstrap
only; do not bypass human review, force-push the default branch, enable auto-merge
or deploy as part of a test/PR handoff. Actual human GitHub approval is required
before merge. A local pre-push barrier is a convenience, not server-side branch
protection or an excuse to omit exact-source review.

Run both README verification commands with a fresh pinned native source and a
disposable isolated test venv. Reject skips, incomplete discovery and wrong/dirty
source. The inherited plain-assert tests must run through the assertion-preserving
adapter, especially under `-O`; do not claim direct optimized script execution
checks the original assertions.

Fixtures must use owned temporary roots, explicit synthetic identities and
canonical boards created there. Preserve native security/delegation markers;
never fabricate dispatcher/worker authority, touch live boards, rewrite private
policies or provision a live selected dependency generation to make tests green.
Source-only native registration/lifecycle tests are not installation admission or
production readiness. Label mocked instrumentation and synthetic lifecycle inputs
honestly, and keep real native transaction effects confined to disposable boards.

Stage an exact public bundle, check whitespace, compile/test the staged export,
and inspect staged blobs plus every unpublished commit for private inputs. Keep
credentials, topology, environment, stores, receipts and private workspace history
out of the public tree. Private audit receipts stay outside the source checkout.

For new/changed functions, prefer complexity below 10; above 15 requires
justification or refactoring. Existing high-complexity engine functions are
preserved to avoid silently changing behavior during extraction. Any future
refactor needs explicit equivalence and boundary regressions, not cosmetic edits.

After user-authorized installation, keep one reviewed integration checkout under
`~/source/repos`; profile paths can link to it. Feature writers must not advance
a live source checkout without an explicit reviewed promotion boundary. Activation,
consumer scheduling and polling reduction remain separate operator decisions.
