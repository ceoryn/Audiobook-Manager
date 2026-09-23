# Audiobook Manager project rules

These rules apply to every change in this repository.

## Safety invariants

- Original audiobook files are sacred. Never overwrite or permanently delete them.
- Scanning and analysis are read-only with respect to the audiobook library.
- New media must be written to a separate temporary/output location and validated before it can supersede anything.
- Risky operations must support a visible dry-run plan and require explicit user approval.
- Cleanup defaults to quarantine/archive. Permanent deletion, if ever implemented, must be a separate explicit action and never the default.
- Low-confidence or conflicting evidence goes to review; never guess.
- Every proposed rename, move, conversion, or cleanup action must be explainable and logged.
- Operations should be resumable and idempotent where practical.
- Tests must use generated fixtures or temporary files, never the user's real library.
- Do not weaken a safety invariant to simplify implementation.

## Engineering rules

- Keep the CLI and test suite runnable after each meaningful milestone.
- Use type hints and small, cohesive modules.
- One corrupt file must not abort a scan.
- Schema changes require a migration/version strategy.
- Network providers must be optional, cached, rate-conscious, and failure-tolerant.
- Do not expose commands that are placeholders.
