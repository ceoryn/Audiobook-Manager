from __future__ import annotations

import threading
from pathlib import Path

from .configuration import require_outside_source, save_configuration, validate_library_paths
from .database import StateDatabase
from .engine import process_library


class StopRequested(RuntimeError):
    """Cooperative cancellation raised only at safe processing checkpoints."""


class ProcessController:
    def __init__(
        self,
        database_path: Path,
        source: Path | None = None,
        destination: Path | None = None,
        *,
        prefer_latin_metadata: bool = False,
    ) -> None:
        self.database_path, self.source, self.destination = database_path, source, destination
        if source is not None:
            require_outside_source(database_path, source, purpose="state database")
        self.prefer_latin_metadata = prefer_latin_metadata
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pause = threading.Event()
        self._stop = threading.Event()

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            if self.source is None or self.destination is None:
                raise ValueError("choose a source folder and output folder before starting")
            self.source, self.destination = validate_library_paths(
                self.source, self.destination,
            )
            self._pause.clear()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="audiobook-process", daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            if self.source is None or self.destination is None:
                raise ValueError("source and output folders are not configured")
            process_library(
                self.source,
                self.destination,
                self.database_path,
                prefer_latin_metadata=self.prefer_latin_metadata,
                checkpoint=self._checkpoint,
            )
        except StopRequested:
            with StateDatabase(self.database_path) as database:
                run = database.latest_process_status()
                if run:
                    run_id = int(run["id"])
                    database.update_process_run(run_id, "stopped")
                    database.log_event(
                        run_id,
                        "run_stopped",
                        level="warning",
                        detail="Stopped by user at a safe checkpoint; run remains resumable",
                    )
        except Exception as exc:
            with StateDatabase(self.database_path) as database:
                run = database.latest_process_status()
                if run:
                    run_id = int(run["id"])
                    database.update_process_run(run_id, "failed")
                    database.log_event(
                        run_id, "run_failed", level="error", detail=str(exc),
                    )

    def _checkpoint(self) -> None:
        if self._stop.is_set():
            raise StopRequested("processing stopped by user")
        while self._pause.is_set():
            if self._stop.wait(0.25):
                raise StopRequested("processing stopped by user")

    def pause(self) -> bool:
        if not self._thread or not self._thread.is_alive():
            return False
        self._pause.set()
        with StateDatabase(self.database_path) as database:
            run = database.latest_process_status()
            if run:
                database.log_event(int(run["id"]), "run_paused")
        return True

    def resume(self) -> bool:
        if not self._thread or not self._thread.is_alive():
            return False
        self._pause.clear()
        with StateDatabase(self.database_path) as database:
            run = database.latest_process_status()
            if run:
                database.log_event(int(run["id"]), "run_resumed")
        return True

    def stop(self) -> bool:
        if not self._thread or not self._thread.is_alive():
            return False
        self._stop.set()
        self._pause.clear()
        with StateDatabase(self.database_path) as database:
            run = database.latest_process_status()
            if run:
                database.log_event(
                    int(run["id"]), "stop_requested", level="warning",
                )
        return True

    def configure(
        self, source: Path, destination: Path, *, config_path: Path | None = None,
    ) -> None:
        resolved_source, resolved_destination = validate_library_paths(source, destination)
        require_outside_source(self.database_path, resolved_source, purpose="state database")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("folders cannot be changed while processing is running")
            if config_path is not None:
                save_configuration(config_path, resolved_source, resolved_destination)
            self.source = resolved_source
            self.destination = resolved_destination

    def status(self) -> dict[str, object]:
        with StateDatabase(self.database_path) as database:
            persisted = database.latest_process_status()
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "paused": self._pause.is_set(),
            "configured": self.source is not None and self.destination is not None,
            "source": str(self.source) if self.source is not None else None,
            "destination": str(self.destination) if self.destination is not None else None,
            "run": persisted,
        }
