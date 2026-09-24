#!/usr/bin/env python3
"""Apply an approved no-overwrite relocation of one newly imported M4B."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.display import person_key
from scripts.apply_remote_to_local_import import save_json_atomic, sha256_file


def apply(plan: dict[str, Any], ledger_path: Path) -> dict[str, Any]:
    if plan.get("mode") != "dry-run":
        raise ValueError("expected a reviewed dry-run relocation plan")
    root = Path(plan["destination_root"]).resolve()
    source = Path(plan["source"])
    target = Path(plan["destination"])
    if not root.is_dir() or not root.parent.is_mount():
        raise ValueError("destination filesystem is not present")
    if os.statvfs(root).f_flag & os.ST_RDONLY:
        raise ValueError("destination is mounted read-only")
    if (not source.is_absolute() or not target.is_absolute() or
            not source.resolve().is_relative_to(root) or
            not target.resolve().is_relative_to(root)):
        raise ValueError("source or destination escapes the configured audiobook root")
    source_parts = source.relative_to(root).parts
    target_parts = target.relative_to(root).parts
    if person_key(source_parts[0]) != person_key(target_parts[0]):
        raise ValueError("relocation changes author identity")
    if (plan["asin"] not in source.name or plan["asin"] not in target.name or
            source.suffix.casefold() != ".m4b" or target.suffix.casefold() != ".m4b"):
        raise ValueError("ASIN or media extension differs")
    ledger = json.loads(ledger_path.read_text())
    record = ledger.get(str(source))
    if not record or record.get("asin") != plan["asin"] or record.get("size") != plan["size"]:
        raise ValueError("source is not the approved ledger-tracked import")
    if record.get("sha256") != plan["sha256"] or not source.is_file() or source.is_symlink():
        raise ValueError("source or ledger hash does not match plan")
    if source.stat().st_size != plan["size"] or sha256_file(source) != plan["sha256"]:
        raise ValueError("source content differs from approved plan")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError("destination is on a different filesystem")
    os.link(source, target)
    updated = dict(record)
    updated["relocated_from"] = str(source)
    updated["relocated_at"] = datetime.now().astimezone().isoformat()
    ledger[str(target)] = updated
    del ledger[str(source)]
    save_json_atomic(ledger_path, ledger)
    source.unlink()
    removed_dirs = []
    parent = source.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        removed_dirs.append(str(parent))
        parent = parent.parent
    return {"source": str(source), "destination": str(target),
            "sha256": plan["sha256"], "empty_directories_removed": removed_dirs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if not args.approved:
        print(f"Dry run only: {plan['source']} -> {plan['destination']}")
        return
    print(json.dumps(apply(plan, args.ledger), indent=2))


if __name__ == "__main__":
    main()
