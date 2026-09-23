# Architecture

## Processing flow

```text
Web UI or CLI
    -> validate separate source/output boundaries
    -> recursively scan supported audio without modifying it
    -> cache stat and ffprobe evidence in versioned SQLite
    -> detect logical books and competing representations
    -> reconcile complete files, parts, discs, and legacy conversions
    -> query cached, failure-tolerant metadata providers
    -> retain low-confidence results for review
    -> copy, remux, or convert into a temporary output
    -> verify duration, streams, chapters, identity, and output ownership
    -> atomically publish the new M4B or quarantine the problem
```

Each book is isolated. A corrupt file, provider outage, or failed conversion is
recorded without aborting unrelated work. Process runs and book states are persisted,
which makes reruns idempotent and interrupted work resumable.

## Local web application

The Python HTTP API binds to localhost by default. It owns the processing controller,
SQLite connection lifecycle, folder validation, directory listing, and local JSON
configuration. The React dashboard polls that API for status and exposes folder
selection, start, pause, resume, stop, and diagnostic-log controls.

`audiobook-manager.json` is local runtime state and is ignored by Git. A new install
may start without it. Saving source and output locations in the UI writes the file
atomically. Configuration cannot change while a processing thread is active.

The folder browser returns directory names and permissions only. It does not expose
file contents and is not intended to be bound to a public network interface.

## Core modules

- `configuration.py`: portable local configuration, path-boundary checks, and folder browsing.
- `controller.py`: background processing lifecycle and cooperative pause/stop checkpoints.
- `scanner.py` and `probe.py`: deterministic discovery and bounded `ffprobe` inspection.
- `grouping.py`, `detection.py`, and `reconcile.py`: book identity and representation selection.
- `hints.py`, `providers.py`, and `matching.py`: metadata evidence and provider matching.
- `conversion.py`, `executor.py`, and `output.py`: safe build, verification, and promotion.
- `database.py`: versioned SQLite schema, cache, process state, output claims, and event log.
- `webserver.py`: localhost JSON API.
- `cli.py`: command-line interface and service entry point.

## State and migrations

SQLite `PRAGMA user_version` is the schema version. Migrations are incremental and
transactional. A database created by a newer application version is rejected with a
clear explanation rather than modified. Media cache fingerprints use resolved path,
byte size, and nanosecond modification time; expensive content hashes are reserved
for operations that require exact identity.

## Safety boundaries

- The source tree is read-only application input.
- Source and output must be separate and non-nested.
- New media is staged beneath the output filesystem and verified before publication.
- Existing outputs require identity verification before reuse and are never silently replaced.
- Every detected book has its own persisted state and evidence.
- Cleanup utilities produce dry-run plans and default to reversible quarantine.
- Tests use generated temporary fixtures, never a real library.
