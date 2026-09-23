"""Inventory output folders; optionally archive artwork-only leaves and prune empty folders."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

ART = {".jpg", ".jpeg", ".png", ".webp"}


def inventory(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    empty, artwork = [], []
    for directory, dirs, files in os.walk(root, followlinks=False):
        path = Path(directory)
        dirs[:] = [d for d in dirs if not d.startswith(("_", "."))
                   and not (path / d).is_symlink()]
        if path == root or path.is_symlink():
            continue
        entries = list(path.iterdir())
        relative = str(path.relative_to(root))
        if not entries:
            empty.append({"path": str(path), "relative_path": relative})
        elif all(p.is_file() and not p.is_symlink() and p.suffix.lower() in ART
                 for p in entries):
            artwork.append({"path": str(path), "relative_path": relative,
                            "files": [{"name": p.name, "size": p.stat().st_size,
                                       "mtime_ns": p.stat().st_mtime_ns}
                                      for p in sorted(entries)]})
    return {"mode": "read-only", "output_root": str(root),
            "summary": {"directories_removed": 0, "empty": len(empty),
                        "artwork_only": len(artwork)},
            "empty_directories": empty, "artwork_only_directories": artwork}


def apply(plan: dict[str, Any], log_path: Path) -> dict[str, Any]:
    if plan.get("mode") != "read-only":
        raise ValueError("requires a read-only inventory")
    root = Path(plan["output_root"]).resolve(strict=True)
    archive = root / "_quarantine" / ("folder-hygiene-" + plan["stamp"])
    result: dict[str, Any] = {"mode": "applied", "source_writes": 0,
                              "files_deleted": 0, "archived": [], "removed_empty": [],
                              "skipped": [], "archive": str(archive)}

    def log() -> None:
        temporary = log_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        temporary.replace(log_path)

    for item in plan["artwork_only_directories"]:
        relative = Path(item["relative_path"])
        source = root / relative
        destination = archive / relative
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or any(part.startswith(("_", ".")) for part in relative.parts)
                or source.is_symlink() or source.resolve() != source):
            raise ValueError(f"unsafe path: {source}")
        if not source.exists() and destination.is_dir():
            continue
        expected = {p["name"]: (p["size"], p["mtime_ns"]) for p in item["files"]}
        actual = list(source.iterdir()) if source.is_dir() else []
        if (not actual or destination.exists()
                or any(p.is_symlink() or not p.is_file() or p.suffix.lower() not in ART
                       for p in actual)
                or {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in actual} != expected):
            result["skipped"].append(str(relative))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        result["pending_move"] = {"source": str(source), "destination": str(destination)}
        log()
        source.rename(destination)
        result["archived"].append(str(relative))
        result.pop("pending_move", None)
        log()
    # rmdir atomically refuses any folder that contains even a hidden file.
    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
        path = Path(directory)
        relative = path.relative_to(root)
        if not relative.parts or any(p.startswith(("_", ".")) for p in relative.parts):
            continue
        if path.is_symlink() or path.resolve() != path:
            continue
        try:
            path.rmdir()
        except OSError:
            continue
        result["removed_empty"].append(str(relative))
        log()
    result["remaining"] = inventory(root)["summary"]
    log()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        if not args.plan:
            parser.error("--apply requires --plan")
        result = apply(json.loads(args.plan.read_text()), args.plan.with_suffix(".applied.json"))
        print(json.dumps({"artwork_folders_archived": len(result["archived"]),
                          "empty_directories_removed": len(result["removed_empty"]),
                          "remaining": result["remaining"], "archive": result["archive"]}, indent=2))
    else:
        if not args.output_root:
            parser.error("inventory requires --output-root")
        plan = inventory(args.output_root)
        plan["stamp"] = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        target = Path("reports") / f"output-folders-{plan['stamp']}.json"
        target.write_text(json.dumps(plan, indent=2) + "\n")
        print(json.dumps({"report": str(target), **plan["summary"]}, indent=2))


if __name__ == "__main__":
    main()
