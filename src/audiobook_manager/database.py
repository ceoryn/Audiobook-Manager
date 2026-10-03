from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .models import ProbeResult, ScannedFile

SCHEMA_VERSION = 10
DATABASE_BUSY_TIMEOUT_MS = 30_000
DATABASE_BUSY_RETRY_ATTEMPTS = 5
DATABASE_BUSY_RETRY_DELAY_SECONDS = 0.05

_T = TypeVar("_T")


class StateDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> StateDatabase:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            timeout=DATABASE_BUSY_TIMEOUT_MS / 1000,
        )
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute(f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}")
            journal_mode = self.connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(journal_mode).casefold() != "wal":
                self._retry_locked(
                    lambda: self._db().execute("PRAGMA journal_mode = WAL").fetchone()
                )
            self.connection.execute("PRAGMA synchronous = NORMAL")
            self._retry_locked(self._migrate)
        except Exception:
            self.connection.close()
            self.connection = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("database is not open")
        return self.connection

    @staticmethod
    def _is_locked_error(error: sqlite3.OperationalError) -> bool:
        code = getattr(error, "sqlite_errorcode", None)
        if isinstance(code, int) and (code & 0xFF) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            return True
        message = str(error).casefold()
        return "database is locked" in message or "database table is locked" in message

    def _retry_locked(self, operation: Callable[[], _T]) -> _T:
        """Retry transient SQLite writer contention without hiding other failures."""
        for attempt in range(DATABASE_BUSY_RETRY_ATTEMPTS):
            try:
                return operation()
            except sqlite3.OperationalError as error:
                if not self._is_locked_error(error) or attempt == DATABASE_BUSY_RETRY_ATTEMPTS - 1:
                    raise
                try:
                    self._db().rollback()
                except sqlite3.Error:
                    pass
                time.sleep(DATABASE_BUSY_RETRY_DELAY_SECONDS * (2**attempt))
        raise RuntimeError("unreachable SQLite retry state")

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        def transact() -> _T:
            database = self._db()
            with database:
                return operation(database)

        return self._retry_locked(transact)

    def _migrate(self) -> None:
        db = self._db()
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"state database schema {version} is newer than supported schema {SCHEMA_VERSION}"
            )
        if version == 0:
            with db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_files (
                        path TEXT PRIMARY KEY,
                        size INTEGER NOT NULL,
                        modified_ns INTEGER NOT NULL,
                        probe_json TEXT,
                        error TEXT,
                        scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                db.execute("PRAGMA user_version = 1")
            version = 1
        if version == 1:
            with db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_decisions (
                        relationship_id TEXT PRIMARY KEY,
                        decision TEXT NOT NULL CHECK(decision IN ('confirm', 'reject', 'defer')),
                        evidence_fingerprint TEXT NOT NULL,
                        group_key TEXT NOT NULL,
                        note TEXT,
                        decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                db.execute("PRAGMA user_version = 2")
            version = 2
        if version == 2:
            with db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS approved_metadata (
                        relationship_id TEXT PRIMARY KEY,
                        metadata_json TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                db.execute("PRAGMA user_version = 3")
            version = 3
        if version == 3:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS batch_runs (
                    id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    approved_at TEXT)""")
                db.execute("""CREATE TABLE IF NOT EXISTS batch_items (
                    run_id INTEGER NOT NULL, relationship_id TEXT NOT NULL,
                    status TEXT NOT NULL, output_path TEXT, reason TEXT,
                    PRIMARY KEY(run_id, relationship_id),
                    FOREIGN KEY(run_id) REFERENCES batch_runs(id))""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_batch_items_run_status ON batch_items(run_id, status)")
                db.execute("PRAGMA user_version = 4")
            version = 4
        if version == 4:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS process_runs (
                    id INTEGER PRIMARY KEY, source_root TEXT NOT NULL, destination_root TEXT NOT NULL,
                    status TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT, summary_json TEXT NOT NULL DEFAULT '{}')""")
                db.execute("""CREATE TABLE IF NOT EXISTS detected_books (
                    run_id INTEGER NOT NULL, book_id TEXT NOT NULL, state TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL, classification TEXT NOT NULL,
                    files_json TEXT NOT NULL, metadata_json TEXT, candidates_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL DEFAULT '[]', confidence REAL NOT NULL DEFAULT 0,
                    output_path TEXT, failure TEXT, quarantine_path TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(run_id, book_id), FOREIGN KEY(run_id) REFERENCES process_runs(id))""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_detected_books_run_state ON detected_books(run_id, state)")
                db.execute("PRAGMA user_version = 5")
            version = 5
        if version == 5:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS metadata_cache (
                    provider TEXT NOT NULL, query TEXT NOT NULL, response_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(provider, query))""")
                db.execute("PRAGMA user_version = 6")
            version = 6
        if version == 6:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS process_events (
                    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, level TEXT NOT NULL,
                    event TEXT NOT NULL, book_id TEXT, detail TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(run_id) REFERENCES process_runs(id))""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_process_events_run_id ON process_events(run_id, id)")
                db.execute("PRAGMA user_version = 7")
            version = 7
        if version == 7:
            with db:
                db.execute(
                    "ALTER TABLE detected_books ADD COLUMN alternate_files_json TEXT NOT NULL DEFAULT '[]'"
                )
                db.execute(
                    "ALTER TABLE detected_books ADD COLUMN problem_files_json TEXT NOT NULL DEFAULT '[]'"
                )
                db.execute("PRAGMA user_version = 8")
            version = 8
        if version == 8:
            with db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS output_claims (
                        run_id INTEGER NOT NULL,
                        output_path TEXT NOT NULL,
                        book_id TEXT NOT NULL,
                        claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(run_id, output_path),
                        FOREIGN KEY(run_id) REFERENCES process_runs(id)
                    )"""
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_output_claims_book ON output_claims(run_id, book_id)"
                )
                db.execute("PRAGMA user_version = 9")
            version = 9
        if version == 9:
            with db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS archive_imports (
                        source_root TEXT NOT NULL,
                        destination_root TEXT NOT NULL,
                        archive_relative_path TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        modified_ns INTEGER NOT NULL,
                        members_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        output_path TEXT NOT NULL,
                        duration_seconds REAL NOT NULL,
                        chapter_count INTEGER NOT NULL,
                        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(source_root, destination_root, archive_relative_path),
                        UNIQUE(source_root, destination_root, output_path)
                    )"""
                )
                db.execute("PRAGMA user_version = 10")

    def cached_probe(
        self, path: Path, *, size: int, modified_ns: int
    ) -> tuple[ProbeResult | None, str | None] | None:
        row = self._db().execute(
            "SELECT probe_json, error FROM media_files WHERE path = ? AND size = ? AND modified_ns = ?",
            (str(path), size, modified_ns),
        ).fetchone()
        if row is None:
            return None
        probe = ProbeResult.from_dict(json.loads(row["probe_json"])) if row["probe_json"] else None
        return probe, row["error"]

    def store(self, item: ScannedFile) -> None:
        probe_json = json.dumps(item.probe.to_dict(), sort_keys=True) if item.probe else None
        self._write(
            lambda database: database.execute(
                """
                INSERT INTO media_files(path, size, modified_ns, probe_json, error, scanned_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    modified_ns = excluded.modified_ns,
                    probe_json = excluded.probe_json,
                    error = excluded.error,
                    scanned_at = CURRENT_TIMESTAMP
                """,
                (str(item.path), item.size, item.modified_ns, probe_json, item.error),
            )
        )

    def store_decision(
        self,
        *,
        relationship_id: str,
        decision: str,
        evidence_fingerprint: str,
        group_key: str,
        note: str | None = None,
    ) -> None:
        if decision not in {"confirm", "reject", "defer"}:
            raise ValueError(f"invalid decision: {decision}")
        self._write(
            lambda database: database.execute(
                """
                INSERT INTO user_decisions(
                    relationship_id, decision, evidence_fingerprint, group_key, note,
                    decided_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(relationship_id) DO UPDATE SET
                    decision = excluded.decision,
                    evidence_fingerprint = excluded.evidence_fingerprint,
                    group_key = excluded.group_key,
                    note = excluded.note,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (relationship_id, decision, evidence_fingerprint, group_key, note),
            )
        )

    def decisions(self) -> dict[str, dict[str, str | None]]:
        rows = self._db().execute(
            "SELECT relationship_id, decision, evidence_fingerprint, group_key, note, updated_at FROM user_decisions"
        ).fetchall()
        return {
            row["relationship_id"]: {
                "decision": row["decision"],
                "evidence_fingerprint": row["evidence_fingerprint"],
                "group_key": row["group_key"],
                "note": row["note"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def approve_metadata(self, relationship_id: str, metadata: dict[str, object]) -> None:
        title = str(metadata.get("title", "")).strip()
        provider = str(metadata.get("provider", "")).strip()
        provider_id = str(metadata.get("provider_id", "")).strip()
        if not title or not provider or not provider_id:
            raise ValueError("title, provider, and provider_id are required")
        self._write(
            lambda database: database.execute(
                """INSERT INTO approved_metadata(relationship_id, metadata_json, provider, provider_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(relationship_id) DO UPDATE SET metadata_json=excluded.metadata_json,
                   provider=excluded.provider, provider_id=excluded.provider_id, updated_at=CURRENT_TIMESTAMP""",
                (relationship_id, json.dumps(metadata, sort_keys=True), provider, provider_id),
            )
        )

    def approved_metadata(self) -> dict[str, dict[str, object]]:
        rows = self._db().execute("SELECT relationship_id, metadata_json FROM approved_metadata").fetchall()
        return {row["relationship_id"]: json.loads(row["metadata_json"]) for row in rows}

    def create_batch_plan(self, items: list[dict[str, str | None]]) -> int:
        def create(database: sqlite3.Connection) -> int:
            cursor = database.execute("INSERT INTO batch_runs(status) VALUES ('draft')")
            run_id = int(cursor.lastrowid)
            database.executemany(
                "INSERT INTO batch_items(run_id, relationship_id, status, output_path, reason) VALUES (?, ?, ?, ?, ?)",
                [(run_id, item["relationship_id"], item["status"], item.get("output_path"), item.get("reason")) for item in items],
            )
            return run_id

        return self._write(create)

    def batch_plan(self, run_id: int) -> dict[str, object] | None:
        run = self._db().execute("SELECT id, status, created_at, approved_at FROM batch_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            return None
        rows = self._db().execute("SELECT relationship_id, status, output_path, reason FROM batch_items WHERE run_id=? ORDER BY relationship_id", (run_id,)).fetchall()
        return {"id": run["id"], "status": run["status"], "created_at": run["created_at"],
                "approved_at": run["approved_at"], "items": [dict(row) for row in rows]}

    def start_process_run(self, source: Path, destination: Path) -> int:
        def create(database: sqlite3.Connection) -> int:
            cursor = database.execute(
                "INSERT INTO process_runs(source_root, destination_root, status) VALUES (?, ?, 'discovering')",
                (str(source.resolve()), str(destination.resolve())),
            )
            return int(cursor.lastrowid)

        return self._write(create)

    def resumable_process_run(self, source: Path, destination: Path) -> int | None:
        row = self._db().execute(
            """SELECT id FROM process_runs WHERE source_root=? AND destination_root=?
               AND status NOT IN ('complete', 'failed') ORDER BY id DESC LIMIT 1""",
            (str(source.resolve()), str(destination.resolve())),
        ).fetchone()
        return int(row["id"]) if row else None

    def update_process_run(self, run_id: int, status: str, summary: dict[str, object] | None = None) -> None:
        self._write(
            lambda database: database.execute(
                """UPDATE process_runs SET status=?, summary_json=?,
                   finished_at=CASE WHEN ?='complete' THEN CURRENT_TIMESTAMP ELSE finished_at END WHERE id=?""",
                (status, json.dumps(summary or {}, sort_keys=True), status, run_id),
            )
        )

    def process_run(self, run_id: int) -> dict[str, object] | None:
        row = self._db().execute("SELECT * FROM process_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_process_status(self) -> dict[str, object] | None:
        run = self._db().execute("SELECT * FROM process_runs ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            return None
        counts = self._db().execute(
            "SELECT state, COUNT(*) AS count FROM detected_books WHERE run_id=? AND state!='superseded' GROUP BY state",
            (run["id"],),
        ).fetchall()
        failures = self._db().execute(
            "SELECT book_id, state, substr(failure, 1, 4000) AS failure, quarantine_path FROM detected_books WHERE run_id=? AND state IN ('failed','quarantined') ORDER BY book_id LIMIT 100",
            (run["id"],),
        ).fetchall()
        return {**dict(run), "summary": json.loads(run["summary_json"]),
                "counts": {row["state"]: row["count"] for row in counts},
                "problems": [dict(row) for row in failures]}

    def log_event(self, run_id: int, event: str, *, level: str = "info",
                  book_id: str | None = None, detail: str | None = None) -> None:
        self._write(
            lambda database: database.execute(
                "INSERT INTO process_events(run_id, level, event, book_id, detail) VALUES (?, ?, ?, ?, ?)",
                (run_id, level, event, book_id, detail),
            )
        )

    def process_log(self, run_id: int, limit: int = 5000) -> list[dict[str, object]]:
        rows = self._db().execute(
            "SELECT id, created_at, level, event, book_id, detail FROM process_events WHERE run_id=? ORDER BY id LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def supersede_missing_books(self, run_id: int, active_ids: set[str]) -> int:
        """Retain obsolete detector identities as audit history but remove them from work queues."""
        rows = self._db().execute(
            "SELECT book_id FROM detected_books WHERE run_id=? AND state!='superseded'", (run_id,)
        ).fetchall()
        missing = [str(row["book_id"]) for row in rows if str(row["book_id"]) not in active_ids]
        if missing:
            self._write(
                lambda database: database.executemany(
                    """UPDATE detected_books SET state='superseded',
                       failure='superseded by improved detector identity', updated_at=CURRENT_TIMESTAMP
                       WHERE run_id=? AND book_id=?""",
                    [(run_id, book_id) for book_id in missing],
                )
            )
        return len(missing)

    def begin_rediscovery(self, run_id: int) -> int:
        """Hide prior-pass outcomes until each identity is reevaluated in this pass."""
        def supersede(database: sqlite3.Connection) -> int:
            database.execute("DELETE FROM output_claims WHERE run_id=?", (run_id,))
            cursor = database.execute(
                """UPDATE detected_books SET state='superseded',
                   failure='awaiting reevaluation by current discovery pass', updated_at=CURRENT_TIMESTAMP
                   WHERE run_id=? AND state!='superseded'""", (run_id,)
            )
            return int(cursor.rowcount)

        return self._write(supersede)

    def store_detected_book(self, *, run_id: int, book_id: str, state: str,
                            source_fingerprint: str, classification: str,
                            files: list[str], evidence: list[dict[str, object]],
                            alternate_files: list[str] | None = None,
                            problem_files: list[str] | None = None) -> None:
        self._write(
            lambda database: database.execute(
                """INSERT INTO detected_books(run_id, book_id, state, source_fingerprint,
                   classification, files_json, evidence_json, alternate_files_json,
                   problem_files_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, book_id) DO UPDATE SET state=excluded.state,
                   source_fingerprint=excluded.source_fingerprint, classification=excluded.classification,
                   files_json=excluded.files_json, evidence_json=excluded.evidence_json,
                   alternate_files_json=excluded.alternate_files_json,
                   problem_files_json=excluded.problem_files_json,
                   metadata_json=NULL, candidates_json='[]', confidence=0,
                   output_path=NULL, failure=NULL, quarantine_path=NULL,
                   updated_at=CURRENT_TIMESTAMP""",
                (run_id, book_id, state, source_fingerprint, classification,
                 json.dumps(files), json.dumps(evidence),
                 json.dumps(alternate_files or []), json.dumps(problem_files or [])),
            )
        )

    def process_books(self, run_id: int) -> list[dict[str, object]]:
        rows = self._db().execute(
            """SELECT book_id, state, source_fingerprint, classification, files_json,
                      alternate_files_json, problem_files_json, metadata_json,
                      candidates_json, evidence_json, confidence, output_path,
                      failure, quarantine_path
                 FROM detected_books WHERE run_id=? ORDER BY book_id""",
            (run_id,),
        ).fetchall()
        return [{**dict(row), "files": json.loads(row["files_json"]),
                 "alternate_files": json.loads(row["alternate_files_json"]),
                 "problem_files": json.loads(row["problem_files_json"]),
                 "evidence": json.loads(row["evidence_json"]),
                 "candidates": json.loads(row["candidates_json"]),
                 "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None} for row in rows]

    def archive_imports(self, source: Path, destination: Path) -> list[dict[str, object]]:
        rows = self._db().execute(
            """SELECT * FROM archive_imports WHERE source_root=? AND destination_root=?
               ORDER BY archive_relative_path""",
            (str(source.resolve()), str(destination.resolve())),
        ).fetchall()
        return [{**dict(row), "members": json.loads(row["members_json"]),
                 "metadata": json.loads(row["metadata_json"])} for row in rows]

    def register_archive_import(
        self, *, source: Path, destination: Path, archive_relative_path: Path,
        size: int, modified_ns: int, members: list[dict[str, object]],
        metadata: dict[str, object], output_path: Path,
        duration_seconds: float, chapter_count: int,
    ) -> None:
        """Commit provenance only after a newly created output passes validation."""
        if archive_relative_path.is_absolute() or ".." in archive_relative_path.parts:
            raise ValueError("archive path must be relative to the source library")
        if not output_path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("archive output path must remain inside the destination library")
        existing = next((row for row in self.archive_imports(source, destination)
                         if row["archive_relative_path"] == archive_relative_path.as_posix()), None)
        if existing is not None:
            same = (
                existing["size"] == size and existing["modified_ns"] == modified_ns
                and existing["members"] == members and existing["metadata"] == metadata
                and existing["output_path"] == str(output_path.resolve())
                and existing["duration_seconds"] == duration_seconds
                and existing["chapter_count"] == chapter_count
            )
            if same:
                return
            raise ValueError("archive import provenance conflicts with an existing record")
        self._write(lambda database: database.execute(
            """INSERT INTO archive_imports(
                source_root, destination_root, archive_relative_path, size, modified_ns,
                members_json, metadata_json, output_path, duration_seconds, chapter_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(source.resolve()), str(destination.resolve()), str(archive_relative_path),
             size, modified_ns, json.dumps(members, sort_keys=True),
             json.dumps(metadata, sort_keys=True), str(output_path.resolve()),
             duration_seconds, chapter_count),
        ))

    def verified_outputs_for_source(
        self, source_fingerprint: str, *, before_run_id: int
    ) -> list[dict[str, object]]:
        """Return previously verified outputs for an unchanged source identity.

        Only records explicitly marked as verified are eligible. This prevents a
        historical automatic metadata mistake from becoming permanent merely
        because an older output happens to exist.
        """
        rows = self._db().execute(
            """SELECT run_id, book_id, metadata_json, output_path
                 FROM detected_books
                WHERE source_fingerprint=? AND run_id<? AND state='complete'
                  AND metadata_json IS NOT NULL AND output_path IS NOT NULL
                ORDER BY run_id DESC""",
            (source_fingerprint, before_run_id),
        ).fetchall()
        verified: list[dict[str, object]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            if metadata.get("_verified_output") is True:
                verified.append({**dict(row), "metadata": metadata})
        return verified

    def adopt_verified_output(
        self, *, run_id: int, book_id: str, metadata: dict[str, object], output_path: Path,
        selected_files: list[str] | None = None,
    ) -> None:
        """Complete a current record from an independently validated prior output."""
        def adopt(database: sqlite3.Connection) -> None:
            if selected_files is not None:
                row = database.execute(
                    "SELECT files_json, alternate_files_json FROM detected_books WHERE run_id=? AND book_id=?",
                    (run_id, book_id),
                ).fetchone()
                if row is None:
                    raise ValueError("detected book does not exist")
                known = list(dict.fromkeys(json.loads(row["files_json"]) + json.loads(row["alternate_files_json"])))
                if not selected_files or len(set(selected_files)) != len(selected_files) or not set(selected_files) <= set(known):
                    raise ValueError("approved selection is not part of the current source representations")
                database.execute(
                    """UPDATE detected_books SET files_json=?, alternate_files_json=?
                       WHERE run_id=? AND book_id=?""",
                    (json.dumps(selected_files), json.dumps([p for p in known if p not in selected_files]), run_id, book_id),
                )
            database.execute(
                """UPDATE detected_books
                      SET state='complete', metadata_json=?, confidence=1,
                          output_path=?, failure=NULL, quarantine_path=NULL,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE run_id=? AND book_id=?""",
                (json.dumps(metadata, sort_keys=True), str(output_path.resolve()), run_id, book_id),
            )
        self._write(adopt)

    def reserve_output_claim(self, run_id: int, book_id: str, output_path: Path) -> str | None:
        """Atomically reserve one physical output for one detected identity.

        Returns the conflicting book id when another identity already owns the
        path, otherwise ``None``. Re-reserving for the same book is idempotent.
        """
        normalized = str(output_path.resolve())

        def reserve(database: sqlite3.Connection) -> str | None:
            row = database.execute(
                "SELECT book_id FROM output_claims WHERE run_id=? AND output_path=?",
                (run_id, normalized),
            ).fetchone()
            if row is not None:
                owner = str(row["book_id"])
                return None if owner == book_id else owner
            database.execute(
                "INSERT INTO output_claims(run_id, output_path, book_id) VALUES (?, ?, ?)",
                (run_id, normalized, book_id),
            )
            return None

        return self._write(reserve)

    def release_output_claim(self, run_id: int, book_id: str, output_path: Path) -> None:
        normalized = str(output_path.resolve())
        self._write(
            lambda database: database.execute(
                "DELETE FROM output_claims WHERE run_id=? AND output_path=? AND book_id=?",
                (run_id, normalized, book_id),
            )
        )

    def store_book_identification(self, *, run_id: int, book_id: str, state: str,
                                  metadata: dict[str, object] | None,
                                  candidates: list[dict[str, object]], evidence: list[dict[str, object]],
                                  confidence: float, failure: str | None = None) -> None:
        self._write(
            lambda database: database.execute(
                """UPDATE detected_books SET state=?, metadata_json=?, candidates_json=?, evidence_json=?,
                   confidence=?, failure=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND book_id=?""",
                (state, json.dumps(metadata, sort_keys=True) if metadata else None,
                 json.dumps(candidates, sort_keys=True), json.dumps(evidence, sort_keys=True),
                 confidence, failure, run_id, book_id),
            )
        )

    def update_book_state(self, run_id: int, book_id: str, state: str, *,
                          output_path: str | None = None, failure: str | None = None,
                          quarantine_path: str | None = None) -> None:
        self._write(
            lambda database: database.execute(
                "UPDATE detected_books SET state=?, output_path=?, failure=?, quarantine_path=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND book_id=?",
                (state, output_path, failure, quarantine_path, run_id, book_id),
            )
        )

    def cached_metadata(self, provider: str, query: str) -> list[dict[str, object]] | None:
        row = self._db().execute(
            "SELECT response_json FROM metadata_cache WHERE provider=? AND query=?",
            (provider, query),
        ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def cache_metadata(self, provider: str, query: str, items: list[dict[str, object]]) -> None:
        self._write(
            lambda database: database.execute(
                """INSERT INTO metadata_cache(provider, query, response_json) VALUES (?, ?, ?)
                   ON CONFLICT(provider, query) DO UPDATE SET response_json=excluded.response_json,
                   fetched_at=CURRENT_TIMESTAMP""",
                (provider, query, json.dumps(items, sort_keys=True)),
            )
        )
