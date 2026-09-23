#!/usr/bin/env python3
"""Plan recoverable quarantine moves using exact encoded-audio identity.

Container paths, tags, covers, and chapters may differ while the AAC packet
stream is byte-for-byte identical.  This read-only planner hashes only the
primary encoded audio stream and proposes a move only when one untracked M4B
has exactly one current-output match.  It never changes either library tree.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any


def _usable(item: dict[str, Any]) -> tuple[bool, float]:
    probe = item.get("probe") or {}
    duration = float(probe.get("duration_seconds") or 0)
    return bool(not probe.get("error") and probe.get("codec") == "aac" and duration), duration


def _stream_hash(path: Path) -> str | None:
    completed = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode or not value.startswith("SHA256="):
        return None
    return value.split("=", 1)[1].strip().casefold()


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _hash_paths(
    paths: set[Path], *, cache_path: Path, workers: int
) -> dict[Path, str | None]:
    cache = _load_cache(cache_path)
    result: dict[Path, str | None] = {}
    pending: list[Path] = []
    for path in sorted(paths):
        stat = path.stat()
        cached = cache.get(str(path)) or {}
        if (
            cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("sha256")
        ):
            result[path] = str(cached["sha256"])
        else:
            pending.append(path)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_stream_hash, path): path for path in pending}
        for future in as_completed(futures):
            path = futures[future]
            digest = future.result()
            result[path] = digest
            if digest:
                stat = path.stat()
                cache[str(path)] = {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": digest,
                }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(cache_path)
    return result


def build_plan(
    audit: dict[str, Any], *, report_dir: Path, workers: int, tolerance: float
) -> dict[str, Any]:
    output_root = Path(audit["roots"]["output"]).resolve()
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    quarantine_root = output_root / "_quarantine" / f"exact-audio-reconciliation-{stamp}"
    current: list[tuple[float, dict[str, Any]]] = []
    for item in audit.get("current_outputs") or []:
        usable, duration = _usable(item)
        if usable:
            current.append((duration, item))

    candidate_map: dict[Path, list[dict[str, Any]]] = {}
    untracked_by_path: dict[Path, dict[str, Any]] = {}
    hash_paths: set[Path] = set()
    preserved: list[dict[str, Any]] = []
    for item in audit.get("untracked_outputs") or []:
        usable, duration = _usable(item)
        source = Path(item["path"]).resolve()
        if not usable:
            preserved.append({
                "source": str(source),
                "relative_path": item["relative_path"],
                "reason": "untracked output is not readable AAC",
            })
            continue
        candidates = [
            possible for current_duration, possible in current
            if abs(current_duration - duration) <= tolerance
        ]
        if not candidates:
            preserved.append({
                "source": str(source),
                "relative_path": item["relative_path"],
                "reason": f"no current output is within {tolerance:g} seconds",
            })
            continue
        untracked_by_path[source] = item
        candidate_map[source] = candidates
        hash_paths.add(source)
        hash_paths.update(Path(item["path"]).resolve() for item in candidates)

    hashes = _hash_paths(
        hash_paths,
        cache_path=report_dir / "audio-stream-hash-cache.json",
        workers=workers,
    )
    operations: list[dict[str, Any]] = []
    for source, candidates in candidate_map.items():
        item = untracked_by_path[source]
        digest = hashes.get(source)
        matches = [
            candidate for candidate in candidates
            if digest and hashes.get(Path(candidate["path"]).resolve()) == digest
        ]
        if len(matches) != 1:
            preserved.append({
                "source": str(source),
                "relative_path": item["relative_path"],
                "reason": (
                    "no exact current audio-stream match"
                    if not matches
                    else "encoded audio matches more than one current output"
                ),
                "exact_match_count": len(matches),
            })
            continue
        reference = matches[0]
        source_duration = float((item.get("probe") or {})["duration_seconds"])
        reference_duration = float((reference.get("probe") or {})["duration_seconds"])
        operations.append({
            "action": "quarantine_exact_audio_duplicate",
            "source": str(source),
            "relative_path": item["relative_path"],
            "destination": str(quarantine_root / item["relative_path"]),
            "reference_output": reference["path"],
            "reference_book_id": reference["book_id"],
            "audio_stream_sha256": digest,
            "source_duration_seconds": source_duration,
            "reference_duration_seconds": reference_duration,
            "evidence": [
                "primary encoded AAC stream is byte-for-byte identical",
                f"duration difference is {abs(source_duration - reference_duration):.3f} seconds",
                "reference is a current output with no audit issues",
            ],
        })

    operations.sort(key=lambda item: item["relative_path"].casefold())
    preserved.sort(key=lambda item: item["relative_path"].casefold())
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "actions_applied": 0,
        "based_on_audit": audit.get("generated_at"),
        "process_run": audit.get("process_run"),
        "roots": audit.get("roots"),
        "safety": {
            "source_library_writes": False,
            "permanent_deletions_planned": 0,
            "quarantine_root_if_approved": str(quarantine_root),
            "execution_requires_exact_hash_revalidation": True,
        },
        "summary": {
            "untracked_outputs_examined": len(audit.get("untracked_outputs") or []),
            "files_hashed": len(hash_paths),
            "exact_audio_duplicates": len(operations),
            "outputs_preserved": len(preserved),
        },
        "quarantine_operations": operations,
        "preserved_outputs": preserved,
    }


def _write_markdown(plan: dict[str, Any], path: Path) -> None:
    summary = plan["summary"]
    lines = [
        "# Exact-audio reconciliation dry run",
        "",
        f"Generated: {plan['generated_at']}",
        "",
        "**No audiobook was changed. Explicit approval is required.**",
        "",
        f"- Untracked outputs examined: **{summary['untracked_outputs_examined']}**",
        f"- Files stream-hashed: **{summary['files_hashed']}**",
        f"- Exact encoded-audio duplicates: **{summary['exact_audio_duplicates']}**",
        f"- Outputs still preserved: **{summary['outputs_preserved']}**",
        "- Permanent deletions: **0**",
        "",
        "## Exact matches proposed for recoverable quarantine",
        "",
        "| Untracked output | Current book record |",
        "|---|---|",
    ]
    for item in plan["quarantine_operations"]:
        lines.append(f"| {item['relative_path']} | {item['reference_book_id']} |")
    lines += [
        "",
        "Every move will be rehashed immediately before execution. The full JSON companion contains the exact source, reference, digest, and quarantine destination.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-json", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--duration-tolerance", type=float, default=5.0)
    args = parser.parse_args()
    audit = json.loads(args.audit_json.resolve().read_text(encoding="utf-8"))
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    plan = build_plan(
        audit,
        report_dir=report_dir,
        workers=args.workers,
        tolerance=max(0.0, args.duration_tolerance),
    )
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    json_path = report_dir / f"exact-audio-reconciliation-dry-run-{stamp}.json"
    markdown_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_markdown(plan, markdown_path)
    print(json.dumps({"summary": plan["summary"], "json": str(json_path), "markdown": str(markdown_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
