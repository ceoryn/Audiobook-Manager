from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_SCHEMA_VERSION = 1


def require_outside_source(path: Path, source: Path, *, purpose: str) -> None:
    """Reject operational writes into the source, including through symlinks."""
    if path.expanduser().resolve().is_relative_to(source.expanduser().resolve()):
        raise ValueError(f"{purpose} must be outside the source library")


@dataclass(frozen=True)
class AppConfiguration:
    source: Path | None = None
    destination: Path | None = None

    @property
    def complete(self) -> bool:
        return self.source is not None and self.destination is not None


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text).expanduser().resolve() if text else None


def load_configuration(path: Path) -> AppConfiguration:
    config_path = path.expanduser().resolve()
    if not config_path.exists():
        return AppConfiguration()
    payload: Any = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("configuration must contain a JSON object")
    version = payload.get("schema_version", CONFIG_SCHEMA_VERSION)
    if version != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"unsupported configuration schema version: {version}")
    # library_root is accepted for compatibility with early local installations.
    source = _optional_path(payload.get("source_root", payload.get("library_root")))
    destination = _optional_path(payload.get("output_root"))
    return AppConfiguration(source=source, destination=destination)


def validate_library_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    resolved_source = source.expanduser().resolve()
    resolved_destination = destination.expanduser().resolve()
    if not resolved_source.is_dir():
        raise ValueError(f"source folder does not exist or is not a directory: {resolved_source}")
    if (resolved_source == resolved_destination
            or resolved_source in resolved_destination.parents
            or resolved_destination in resolved_source.parents):
        raise ValueError("source and output must be separate, non-nested folders")
    if resolved_destination.exists() and not resolved_destination.is_dir():
        raise ValueError(f"output path exists but is not a directory: {resolved_destination}")
    existing_parent = resolved_destination
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir():
        raise ValueError(f"output folder has no accessible parent: {resolved_destination}")
    if not os.access(existing_parent, os.W_OK | os.X_OK):
        raise ValueError(f"output folder is not writable: {existing_parent}")
    return resolved_source, resolved_destination


def save_configuration(path: Path, source: Path, destination: Path) -> AppConfiguration:
    resolved_source, resolved_destination = validate_library_paths(source, destination)
    config_path = path.expanduser().resolve()
    require_outside_source(config_path, resolved_source, purpose="configuration")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "source_root": str(resolved_source),
        "output_root": str(resolved_destination),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(config_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return AppConfiguration(source=resolved_source, destination=resolved_destination)


def browse_directories(path: Path | None = None) -> dict[str, object]:
    current = (path or Path.home()).expanduser().resolve()
    if not current.is_dir():
        raise ValueError(f"folder does not exist or is not a directory: {current}")
    directories: list[dict[str, object]] = []
    try:
        children = sorted(
            (item for item in current.iterdir() if item.is_dir() and not item.name.startswith(".")),
            key=lambda item: item.name.casefold(),
        )
    except PermissionError as exc:
        raise ValueError(f"folder cannot be read: {current}") from exc
    for child in children:
        try:
            readable = os.access(child, os.R_OK | os.X_OK)
            writable = os.access(child, os.W_OK | os.X_OK)
        except OSError:
            readable = writable = False
        directories.append({
            "name": child.name,
            "path": str(child),
            "readable": readable,
            "writable": writable,
        })
    parent = None if current == current.parent else str(current.parent)
    return {
        "path": str(current),
        "parent": parent,
        "readable": os.access(current, os.R_OK | os.X_OK),
        "writable": os.access(current, os.W_OK | os.X_OK),
        "directories": directories,
    }
