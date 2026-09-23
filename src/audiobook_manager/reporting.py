from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import BookGroup, ScannedFile


def build_report(root: Path, files: list[ScannedFile], groups: list[BookGroup]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "library_root": str(root.expanduser().resolve()),
        "summary": {
            "media_files": len(files),
            "groups": len(groups),
            "probe_errors": sum(item.error is not None for item in files),
            "cache_hits": sum(item.cached for item in files),
        },
        "groups": [group.to_dict() for group in groups],
        "files": [item.to_dict() for item in files],
    }
