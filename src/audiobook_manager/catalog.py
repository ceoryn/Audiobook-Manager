from __future__ import annotations

from pathlib import Path
from typing import Any


def classify_catalog(scan: dict[str, Any]) -> list[dict[str, Any]]:
    files = {item["relative_path"]: item for item in scan.get("files", [])}
    catalog: list[dict[str, Any]] = []
    for group in scan.get("groups", []):
        members = [files[path] for path in group.get("files", []) if path in files]
        suffixes = [Path(item["relative_path"]).suffix.casefold() for item in members]
        errors = [item for item in members if item.get("error")]
        m4bs = sum(suffix in {".m4a", ".m4b"} for suffix in suffixes)
        if errors:
            classification, reason = "needs_attention", f"{len(errors)} file(s) could not be probed"
        elif len(members) == 1 and m4bs == 1:
            classification, reason = "existing_m4b", "single existing M4B/M4A"
        elif len(members) == 1:
            classification, reason = "single_conversion", "single audio file needs M4B conversion"
        elif m4bs == 0:
            classification, reason = "multipart_conversion", f"{len(members)} parts need combining"
        elif m4bs == 1:
            classification, reason = "mixed_with_m4b", "existing M4B plus possible source components"
        else:
            classification, reason = "ambiguous", "multiple M4B files require grouping review"
        catalog.append({"id": group["key"], "groupKey": group["key"], "files": group.get("files", []),
                        "classification": classification, "reason": reason})
    return catalog
