from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .catalog import classify_catalog
from .configuration import browse_directories, load_configuration
from .controller import ProcessController
from .conversion import convert_to_m4b
from .database import StateDatabase
from .metadata import search_open_library
from .review import decision_status, review_items


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def build_review_payload(
    analysis_path: Path,
    database_path: Path,
    output_root: Path | None = None,
) -> dict[str, Any]:
    analysis = _load_json(analysis_path)
    items = review_items(analysis)
    with StateDatabase(database_path) as database:
        decisions = database.decisions()
        approved = database.approved_metadata()
    rendered = []
    for item in items:
        status, stale = decision_status(item, decisions)
        rendered.append({
            "id": item.relationship_id,
            "groupKey": item.group_key,
            "reviewReasons": list(item.review_reasons),
            "status": status,
            "stale": stale,
            "metadata": approved.get(item.relationship_id),
            **item.relationship,
        })
    counts = {
        status: sum(item["status"] == status for item in rendered)
        for status in ("unreviewed", "confirm", "defer", "reject")
    }
    return {
        "libraryRoot": analysis.get("library_root"),
        "outputRoot": str(output_root.resolve()) if output_root else None,
        "analysisSummary": analysis.get("summary", {}),
        "decisionCounts": counts,
        "items": rendered,
        "safety": analysis.get("safety", {}),
    }


def build_batch_plan(scan_path: Path, database_path: Path, output_root: Path) -> dict[str, object]:
    catalog = classify_catalog(_load_json(scan_path))
    with StateDatabase(database_path) as database:
        approved = database.approved_metadata()
    planned: list[dict[str, str | None]] = []
    for item in catalog:
        status, reason, output = "needs_metadata", "no approved metadata", None
        if item["classification"] in {"ambiguous", "mixed_with_m4b", "needs_attention"}:
            status, reason = "skipped", item["reason"]
        elif item["id"] in approved:
            metadata = approved[item["id"]]
            author = ", ".join(metadata.get("authors") or [])
            filename = f"{author + ' - ' if author else ''}{metadata['title']}.m4b"
            output = str(output_root.resolve() / filename)
            status, reason = "ready", None
            if Path(output).exists():
                status, reason = "skipped", "output already exists"
        planned.append({
            "relationship_id": item["id"],
            "status": status,
            "output_path": output,
            "reason": reason,
        })
    with StateDatabase(database_path) as database:
        run_id = database.create_batch_plan(planned)
        plan = database.batch_plan(run_id)
    assert plan is not None
    counts = {
        state: sum(item["status"] == state for item in planned)
        for state in ("ready", "needs_metadata", "skipped")
    }
    return {**plan, "counts": counts}


class ReviewRequestHandler(BaseHTTPRequestHandler):
    analysis_path: Path
    database_path: Path
    scan_path: Path
    config_path: Path
    controller: ProcessController

    def _trusted_request(self) -> bool:
        """Only the local UI and direct local clients may operate this API."""
        try:
            host = urlsplit("http://" + self.headers.get("Host", ""))
            valid_host = host.hostname in {"127.0.0.1", "localhost", "::1"}
            valid_host = valid_host and host.port == self.server.server_port
        except ValueError:
            valid_host = False
        origin = self.headers.get("Origin")
        allowed = {
            f"http://{name}:{port}"
            for name in ("127.0.0.1", "localhost", "[::1]")
            for port in (3000, self.server.server_port)
        }
        if not valid_host or (origin is not None and origin not in allowed):
            self._json({"error": "request must come from the local dashboard"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = b"" if status == HTTPStatus.NO_CONTENT else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        }
        origin = self.headers.get("Origin")
        if origin in {"http://127.0.0.1:3000", "http://localhost:3000", "http://[::1]:3000"}:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._trusted_request():
            return
        self._json({}, HTTPStatus.NO_CONTENT)

    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted_request():
            return
        try:
            if self.path == "/api/health":
                self._json({"status": "ok", "readOnlyMedia": True})
            elif self.path == "/api/process/status":
                self._json(self.controller.status())
            elif self.path == "/api/config":
                self._json(self.controller.status())
            elif self.path.startswith("/api/folders"):
                query = parse_qs(urlsplit(self.path).query)
                requested = query.get("path", [None])[0]
                self._json(browse_directories(Path(requested) if requested else None))
            elif self.path == "/api/process/log":
                status = self.controller.status()
                run = status.get("run")
                events = []
                if isinstance(run, dict):
                    with StateDatabase(self.database_path) as database:
                        events = database.process_log(int(run["id"]))
                self._json({"status": status, "events": events})
            elif self.path == "/api/review":
                self._json(build_review_payload(
                    self.analysis_path, self.database_path, self.controller.destination,
                ))
            elif self.path.startswith("/api/metadata/search?"):
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                self._json({"items": [item.to_dict() for item in search_open_library(query)]})
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        if not self._trusted_request():
            return
        supported_paths = {
            "/api/config",
            "/api/decisions",
            "/api/metadata/approve",
            "/api/convert",
            "/api/batch/plan",
            "/api/process/start",
            "/api/process/pause",
            "/api/process/resume",
            "/api/process/stop",
        }
        if self.path not in supported_paths:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("requests must use application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            if self.path == "/api/config":
                source = Path(str(body.get("source", "")).strip())
                destination = Path(str(body.get("destination", "")).strip())
                if not str(body.get("source", "")).strip():
                    raise ValueError("source folder is required")
                if not str(body.get("destination", "")).strip():
                    raise ValueError("output folder is required")
                self.controller.configure(source, destination, config_path=self.config_path)
                self._json({"saved": True, **self.controller.status()})
                return
            if self.path == "/api/process/start":
                started = self.controller.start()
                self._json({"started": started, **self.controller.status()})
                return
            if self.path.startswith("/api/process/"):
                action = self.path.rsplit("/", 1)[-1]
                changed = getattr(self.controller, action)()
                self._json({"changed": changed, **self.controller.status()})
                return
            if self.path == "/api/batch/plan":
                if self.controller.destination is None:
                    raise ValueError("output folder is not configured")
                self._json(build_batch_plan(
                    self.scan_path,
                    self.database_path,
                    self.controller.destination,
                ))
                return
            relationship_id = str(body["relationshipId"])
            analysis = _load_json(self.analysis_path)
            item = next(
                (
                    candidate
                    for candidate in review_items(analysis)
                    if candidate.relationship_id == relationship_id
                ),
                None,
            )
            if item is None:
                self._json(
                    {"error": "relationship ID not found"}, HTTPStatus.NOT_FOUND,
                )
                return
            if self.path == "/api/metadata/approve":
                metadata = body.get("metadata")
                if not isinstance(metadata, dict):
                    raise ValueError("metadata must be an object")
                with StateDatabase(self.database_path) as database:
                    database.approve_metadata(relationship_id, metadata)
                self._json({"ok": True, "relationshipId": relationship_id})
                return
            if self.path == "/api/convert":
                if self.controller.destination is None:
                    raise ValueError("output folder is not configured")
                with StateDatabase(self.database_path) as database:
                    decisions, approved = database.decisions(), database.approved_metadata()
                state, stale = decision_status(item, decisions)
                if state != "confirm" or stale:
                    raise ValueError("current lineage evidence must be confirmed before conversion")
                metadata = approved.get(relationship_id)
                if metadata is None:
                    raise ValueError("metadata must be approved before conversion")
                library_root = Path(str(analysis["library_root"]))
                if self.controller.source is None or library_root.resolve() != self.controller.source.resolve():
                    raise ValueError("analysis belongs to a different source; scan and review again")
                relationship = item.relationship
                relative_inputs = (
                    [relationship["target_file"]]
                    if relationship.get("target_file")
                    else relationship["source_files"]
                )
                destination = convert_to_m4b(
                    inputs=[library_root / str(path) for path in relative_inputs],
                    library_root=library_root,
                    output_root=self.controller.destination,
                    metadata=metadata,
                )
                self._json({"ok": True, "output": str(destination)})
                return
            decision = str(body["decision"])
            note = str(body.get("note", "")).strip() or None
            if note and len(note) > 2000:
                raise ValueError("note must be 2,000 characters or fewer")
            with StateDatabase(self.database_path) as database:
                database.store_decision(
                    relationship_id=item.relationship_id,
                    decision=decision,
                    evidence_fingerprint=item.evidence_fingerprint,
                    group_key=item.group_key,
                    note=note,
                )
            self._json({
                "ok": True,
                "relationshipId": item.relationship_id,
                "decision": decision,
            })
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        print(f"review-web: {format % args}")


def serve_review_app(
    *,
    analysis_path: Path,
    database_path: Path,
    config_path: Path = Path.cwd() / "audiobook-manager.json",
    source_root: Path | None = None,
    output_root: Path | None = None,
    scan_path: Path = Path.cwd() / "library-scan.json",
    prefer_latin_metadata: bool = False,
    host: str = "127.0.0.1",
    port: int = 8788,
) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("the unauthenticated review API must bind to localhost")
    configuration = load_configuration(config_path)
    source = source_root or configuration.source
    destination = output_root or configuration.destination
    if source is None and analysis_path.exists():
        source = Path(str(_load_json(analysis_path)["library_root"]))
    controller = ProcessController(
        database_path, source, destination,
        prefer_latin_metadata=prefer_latin_metadata,
    )
    handler = type(
        "ConfiguredReviewHandler",
        (ReviewRequestHandler,),
        {
            "analysis_path": analysis_path,
            "database_path": database_path,
            "config_path": config_path,
            "scan_path": scan_path,
            "controller": controller,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Audiobook review API listening on http://{host}:{port}")
    print(
        "Source media is read-only; new media is written only to the configured "
        "output folder."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
