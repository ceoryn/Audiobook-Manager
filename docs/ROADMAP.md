# Roadmap

Audiobook Manager already provides read-only discovery, book detection, metadata
matching, verified M4B output, resumable processing, quarantine, diagnostics, and a
local web dashboard. Future work should preserve those safety guarantees.

## Near term

- A first-run dependency check for FFmpeg, Python, Node.js, and writable output space
- Richer metadata-review and quarantine repair screens
- Configurable worker counts and metadata thresholds in the dashboard
- Cross-platform packaging and a guided Linux installer
- Exportable/importable review decisions without library media

## Later

- Additional optional metadata providers with per-provider rate controls
- More complete cover-art and narrator review
- Notifications for long-running jobs
- A signed release process and automated release artifacts
- Expanded accessibility and keyboard navigation

## Non-goals

- Mutating, renaming, or deleting source media
- Guessing through conflicting identity evidence
- Automatic permanent deletion
- Requiring a hosted service or uploading audiobook contents
