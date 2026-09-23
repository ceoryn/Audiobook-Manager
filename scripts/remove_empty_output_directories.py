#!/usr/bin/env python3
"""Remove only verified-empty directories from an audiobook output tree.

The operation is based on a saved read-only audit.  It uses ``Path.rmdir`` for
every action, so a directory that gained any visible or hidden entry after the
audit is preserved automatically.  Underscore-prefixed internal trees and the
output root itself are never candidates.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _safe_candidate(path: Path, output_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(output_root)
    except ValueError:
        return False
    return bool(relative.parts and not relative.parts[0].startswith("_"))


def remove_empty_directories(
    *, output_root: Path, audited_paths: list[Path]
) -> dict[str, Any]:
    output_root = output_root.resolve()
    removed: list[dict[str, str]] = []
    skipped_nonempty: list[str] = []
    already_absent: list[str] = []

    def remove(path: Path, reason: str) -> bool:
        resolved = path.resolve()
        if not _safe_candidate(resolved, output_root):
            raise ValueError(f"unsafe empty-directory candidate: {path}")
        relative = str(resolved.relative_to(output_root))
        if not resolved.exists():
            already_absent.append(relative)
            return False
        if not resolved.is_dir():
            skipped_nonempty.append(relative)
            return False
        try:
            resolved.rmdir()
        except OSError:
            skipped_nonempty.append(relative)
            return False
        removed.append({"relative_path": relative, "reason": reason})
        return True

    unique = {path.resolve() for path in audited_paths}
    for path in sorted(unique, key=lambda value: (-len(value.parts), str(value).casefold())):
        remove(path, "empty_in_saved_audit")

    # Removing empty leaves can reveal empty parent directories that were not
    # leaves during the audit. Re-scan deepest-first until no more can be pruned.
    while True:
        candidates = sorted(
            (
                path for path in output_root.rglob("*")
                if path.is_dir() and _safe_candidate(path, output_root)
            ),
            key=lambda value: (-len(value.parts), str(value).casefold()),
        )
        removed_this_pass = 0
        for path in candidates:
            try:
                is_empty = not any(path.iterdir())
            except OSError:
                continue
            if is_empty and remove(path, "became_empty_after_descendant_cleanup"):
                removed_this_pass += 1
        if not removed_this_pass:
            break

    remaining = [
        str(path.relative_to(output_root))
        for path in output_root.rglob("*")
        if path.is_dir()
        and _safe_candidate(path, output_root)
        and not any(path.iterdir())
    ]
    return {
        "directories_removed": len(removed),
        "audited_directories_removed": sum(
            item["reason"] == "empty_in_saved_audit" for item in removed
        ),
        "newly_empty_parent_directories_removed": sum(
            item["reason"] == "became_empty_after_descendant_cleanup" for item in removed
        ),
        "skipped_because_no_longer_empty": sorted(set(skipped_nonempty), key=str.casefold),
        "already_absent": sorted(set(already_absent), key=str.casefold),
        "remaining_empty_directories": sorted(remaining, key=str.casefold),
        "removed": removed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audit_path = args.audit.expanduser().resolve()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("mode") != "read-only":
        raise ValueError("cleanup requires a saved read-only empty-directory audit")
    if int((audit.get("summary") or {}).get("directories_removed") or 0) != 0:
        raise ValueError("cleanup audit already records applied directory removal")
    output_root = Path(str(audit.get("output_root") or "")).resolve()
    if not output_root.is_dir():
        raise ValueError(f"output root does not exist: {output_root}")
    audited_paths = [Path(item["path"]) for item in audit.get("empty_directories") or []]
    result = remove_empty_directories(output_root=output_root, audited_paths=audited_paths)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "applied",
        "based_on_audit": str(audit_path),
        "output_root": str(output_root),
        "files_removed": 0,
        "source_library_writes": False,
        **result,
    }
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"empty-output-cleanup-{stamp}.json"
    markdown_path = report_dir / f"empty-output-cleanup-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# Empty output-directory cleanup",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        f"- Directories removed: **{payload['directories_removed']}**",
        f"- Audited empty leaves removed: **{payload['audited_directories_removed']}**",
        f"- Newly empty parent directories removed: **{payload['newly_empty_parent_directories_removed']}**",
        f"- Audited paths preserved because they were no longer empty: **{len(payload['skipped_because_no_longer_empty'])}**",
        f"- Empty directories remaining: **{len(payload['remaining_empty_directories'])}**",
        "- Files removed: **0**",
        "- Source-library writes: **0**",
        "",
        "## Removed paths",
        "",
        *[f"- `{item['relative_path']}` ({item['reason']})" for item in payload["removed"]],
        "",
    ]
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "directories_removed": payload["directories_removed"],
        "files_removed": 0,
        "remaining_empty_directories": len(payload["remaining_empty_directories"]),
        "json": str(json_path),
        "markdown": str(markdown_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
