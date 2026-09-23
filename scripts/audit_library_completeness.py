"""Independent physical-source census and source-selection completeness review.

This is read-only for the source, output and live database. Archive members and
unsupported extensions are counted even when the application never detected them.
Matching filename/size is only a candidate, never proof of duplicate audio.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.models import BookGroup, ProbeResult, ScannedFile
from audiobook_manager.probe import ProbeError, media_input_args, probe_media
from audiobook_manager.reconcile import reconcile_group
from audiobook_manager.scanner import SUPPORTED_EXTENSIONS

MEDIA = {".mp3", ".m4b", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}


def archive_inventory(path: Path, root: Path,
                      by_name_size: dict[tuple[str, int], list[str]]) -> dict[str, Any]:
    record: dict[str, Any] = {"path": str(path), "relative_path": str(path.relative_to(root)),
                              "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
                              "members": [], "error": None}
    try:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or Path(entry.filename).suffix.lower() not in MEDIA:
                    continue
                record["members"].append({
                    "name": entry.filename, "size": entry.file_size, "crc32": entry.CRC,
                    "encrypted": bool(entry.flag_bits & 1),
                    "unpacked_name_size_candidates": by_name_size.get(
                        (Path(entry.filename).name.casefold(), entry.file_size), []),
                })
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        record["error"] = str(error)
    record["audio_members"] = len(record["members"])
    record["members_without_unpacked_candidate"] = sum(
        not entry["unpacked_name_size_candidates"] for entry in record["members"])
    return record


def build(database: Path, source: Path, alignment: Path) -> dict[str, Any]:
    if not source.is_dir():
        raise ValueError("source drive must be mounted for a physical completeness audit")
    audit = json.loads(alignment.read_text())
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        run = dict(connection.execute("SELECT * FROM process_runs ORDER BY id DESC LIMIT 1").fetchone())
        if run["id"] != audit["process_run"]["id"] or run["status"] != "complete":
            raise ValueError("alignment audit and latest completed run must agree")
        books = [dict(row) for row in connection.execute(
            "SELECT * FROM detected_books WHERE run_id=? AND state!='superseded'", (run["id"],))]
        cache = {row["path"]: dict(row) for row in connection.execute("SELECT * FROM media_files")}
    finally:
        connection.close()
    by_name_size: dict[tuple[str, int], list[str]] = defaultdict(list)
    files: dict[str, ScannedFile] = {}
    archives, unsupported, uncached, stale, sidecars, suspicious = [], [], [], [], [], []
    refreshed_errors = []
    extensions: Counter[str] = Counter()
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        extensions[suffix] += 1
        relative = str(path.relative_to(source))
        stat = path.stat()
        if suffix in MEDIA:
            by_name_size[(path.name.casefold(), stat.st_size)].append(relative)
        if suffix in {".zip", ".zab"}:
            archives.append(path)
        elif path.name == "metadata.json":
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict):
                    sidecars.append({"path": relative, "title": data.get("title"),
                                     "authors": data.get("authors"),
                                     "chapter_count": len(data.get("chapters") or []),
                                     "chapter_end_seconds": max(
                                         (float(x.get("end") or 0) for x in data.get("chapters") or []),
                                         default=0)})
            except (ValueError, TypeError, OSError):
                suspicious.append({"path": relative, "reason": "unreadable metadata sidecar"})
        if suffix in SUPPORTED_EXTENSIONS:
            cached = cache.get(str(path))
            if cached is None:
                uncached.append(relative)
                record = {"path": relative, "size": stat.st_size,
                          "reason": "physical audio absent from the saved scan"}
                try:
                    record["probe"] = probe_media(path).to_dict()
                    record["error"] = None
                except (OSError, ProbeError) as error:
                    record["error"] = str(error)
                unsupported.append(record)
                continue
            if (cached["size"], cached["modified_ns"]) != (stat.st_size, stat.st_mtime_ns):
                stale.append(relative)
                continue
            probe = ProbeResult.from_dict(json.loads(cached["probe_json"])) if cached["probe_json"] else None
            error = cached["error"]
            if error:
                try:
                    probe = probe_media(path)
                    error = None
                    refreshed_errors.append(relative)
                except (OSError, ProbeError) as failure:
                    error = str(failure)
            files[relative] = ScannedFile(path, Path(relative), stat.st_size,
                                          stat.st_mtime_ns, probe, error, True)
        elif suffix in MEDIA:
            record: dict[str, Any] = {"path": relative, "size": stat.st_size}
            try:
                record["probe"] = probe_media(path).to_dict()
                record["error"] = None
            except (OSError, ProbeError) as error:
                record["error"] = str(error)
            unsupported.append(record)
        elif suffix not in {".zip", ".zab"} and stat.st_size > 1024 * 1024:
            with path.open("rb") as handle:
                header = handle.read(16)
            if (header.startswith((b"ID3", b"fLaC", b"OggS"))
                    or header[4:8] == b"ftyp" or header[:4] == b"RIFF" and header[8:12] == b"WAVE"):
                record = {"path": relative, "reason": "audio/container signature under an unsupported extension"}
                try:
                    record["probe"] = probe_media(path).to_dict()
                except (OSError, ProbeError) as error:
                    record["error"] = str(error)
                suspicious.append(record)

    outputs = {item["book_id"]: item for item in audit["current_outputs"]}
    selection_changes, failures, output_duration_issues, source_errors = [], [], [], []
    incomplete_explicit_sequences = []
    normalized_duration_reviews = []
    corrections: list[dict[str, Any]] = []

    def duration(paths: list[str] | tuple[str, ...]) -> float:
        return sum(float(files[p].probe.duration_seconds or 0) for p in paths
                   if p in files and files[p].probe)

    for book in books:
        selected = json.loads(book["files_json"])
        alternates = json.loads(book["alternate_files_json"])
        problems = json.loads(book["problem_files_json"])
        metadata = json.loads(book["metadata_json"] or "{}")
        if book["state"] == "failed":
            failures.append({"book_id": book["book_id"], "failure": book["failure"],
                             "selected_files": selected})
        current = outputs.get(book["book_id"], {})
        observed = float((current.get("probe") or {}).get("duration_seconds") or 0)
        expected = duration(selected)
        if book["state"] == "complete" and abs(observed - expected) > max(5, expected * .005):
            issue = {"book_id": book["book_id"], "output_duration": observed,
                     "selected_duration": expected}
            if any(media_input_args(source / p) for p in selected):
                issue["reason"] = "legacy RIFF source headers have unreliable duration; compare decoded samples with the recovery log"
                normalized_duration_reviews.append(issue)
            else:
                output_duration_issues.append(issue)
        known = sorted(set(selected + alternates + problems))
        if any(p not in files for p in known):
            continue
        group = BookGroup(book["book_id"], tuple(files[p] for p in known), ())
        result = reconcile_group(group)
        if any(conflict.startswith("explicit ") or
               conflict.startswith("conflicting explicit") or
               conflict.startswith("invalid explicit")
               for conflict in result.conflicts):
            incomplete_explicit_sequences.append({
                "book_id": book["book_id"], "current_state": book["state"],
                "conflicts": list(result.conflicts), "source_files": known,
            })
        proposed = duration(result.chosen_files)
        if result.chosen_files and abs(expected - proposed) > max(5, expected * .01):
            entry = {"book_id": book["book_id"], "state": book["state"],
                     "old_files": selected, "proposed_files": list(result.chosen_files),
                     "old_seconds": expected, "proposed_seconds": proposed,
                     "evidence": list(result.evidence), "conflicts": list(result.conflicts),
                     "metadata": metadata}
            entry["requires_decoded_audio_review"] = any(
                media_input_args(source / p) for p in result.chosen_files)
            selection_changes.append(entry)
            if not entry["requires_decoded_audio_review"]:
                corrections.append({"book_id": book["book_id"], "expected_states": [book["state"]],
                                    "metadata": metadata, "selection": list(result.chosen_files),
                                    "evidence": list(result.evidence)})
    for path, file in files.items():
        if file.error:
            source_errors.append({"path": path, "error": file.error})
    archive_records = [archive_inventory(path, source, by_name_size) for path in archives]
    return {"mode": "read-only", "generated_at": datetime.now().astimezone().isoformat(),
            "based_on_alignment": str(alignment.resolve()), "run_id": run["id"],
            "source_root": str(source), "extension_counts": dict(extensions),
            "summary": {"supported_physical_audio": sum(extensions[x] for x in SUPPORTED_EXTENSIONS),
                        "uncached_audio": len(uncached), "stale_cached_audio": len(stale),
                        "audio_archives": sum(bool(a["audio_members"]) for a in archive_records),
                        "archived_audio_members": sum(a["audio_members"] for a in archive_records),
                        "unsupported_loose_audio": len(unsupported),
                        "selection_runtime_changes": len(selection_changes),
                        "incomplete_explicit_sequences": len(incomplete_explicit_sequences),
                        "repair_plan_candidates": len(corrections),
                        "normalized_source_duration_reviews": len(normalized_duration_reviews),
                        "output_selected_duration_disagreements": len(output_duration_issues),
                        "failed_records": len(failures), "untracked_outputs": len(audit["untracked_outputs"]),
                        "source_probe_errors": len(source_errors), "sidecar_metadata_files": len(sidecars),
                        "previous_probe_errors_recovered": len(refreshed_errors),
                        "audio_under_other_extensions": sum(bool(x.get('probe')) for x in suspicious)},
            "archives": archive_records, "unsupported_audio": unsupported,
            "uncached_audio": uncached, "stale_cache": stale, "suspicious_files": suspicious,
            "previous_probe_errors_recovered": refreshed_errors,
            "selection_changes": selection_changes, "failures": failures,
            "incomplete_explicit_sequences": incomplete_explicit_sequences,
            "output_duration_issues": output_duration_issues, "source_errors": source_errors,
            "normalized_source_duration_reviews": normalized_duration_reviews,
            "sidecar_metadata": sidecars, "untracked_outputs": audit["untracked_outputs"],
            "corrections": {"expected_run_id": run["id"], "repairs": corrections},
            "limitations": ["A decoded or readable file is not proof that the original recording is complete.",
                            "Archive member name/size candidates require audio verification before any deduplication.",
                            "Sidecar metadata is evidence only; some source sidecars identify the wrong book."]}


def markdown(report: dict[str, Any]) -> str:
    lines = ["# Source-to-output completeness review", "", report["generated_at"], "",
             "Read-only audit. Existence, scan coverage, and audio completeness are separate checks.", "",
             "## Counts", ""]
    lines += [f"- {key.replace('_', ' ')}: {value}" for key, value in report["summary"].items()]
    lines += ["", "## Current records with different complete source selections", "",
              "| Book | Current hours | Proposed hours | Reason |", "|---|---:|---:|---|"]
    for entry in report["selection_changes"]:
        detail = '; '.join(entry['evidence'])
        if entry['requires_decoded_audio_review']:
            detail += '; DECODED AUDIO REVIEW REQUIRED: declared duration is unreliable'
        lines.append(f"| {entry['book_id']} | {entry['old_seconds']/3600:.2f} | {entry['proposed_seconds']/3600:.2f} | {detail} |")
    lines += ["", "## Explicitly numbered source sets with missing or unreadable parts", ""]
    for entry in report["incomplete_explicit_sequences"]:
        lines.append(f"- {entry['book_id']} ({entry['current_state']}): {'; '.join(entry['conflicts'])}")
    lines += ["", "## Archives excluded by the scanner", "",
              "These are audio archives, not detected book records. The JSON lists every member and any unpacked filename/size candidates.", ""]
    for archive in report["archives"]:
        lines.append(f"- `{archive['relative_path']}` — {archive['audio_members']} audio members, {archive['members_without_unpacked_candidate']} without an unpacked filename/size candidate.")
    lines += ["", "## Loose audio omitted from the saved scan", ""]
    for item in report["unsupported_audio"]:
        lines.append(f"- `{item['path']}` — " + ("probe failed" if item.get("error") else f"{item['probe']['duration_seconds']:.1f} seconds"))
    lines += ["", "## Audio hidden under other extensions", ""]
    for item in report["suspicious_files"]:
        lines.append(f"- `{item['path']}` — {item['reason']}")
    lines += ["", "## Failed book records", ""]
    for item in report["failures"]:
        lines.append(f"- {item['book_id']}: {item['failure']}")
    lines += ["", "## Untracked output files preserved for reconciliation", ""]
    lines += [f"- `{item['relative_path']}`" for item in report["untracked_outputs"]]
    lines += ["", "## Limits", "", *[f"- {x}" for x in report["limitations"]], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    args = parser.parse_args()
    report = build(args.database, args.source_root.resolve(), args.alignment)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    base = Path("reports") / f"library-completeness-{stamp}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    base.with_suffix(".md").write_text(markdown(report))
    corrections = base.with_suffix(".corrections.json")
    corrections.write_text(json.dumps(report["corrections"], indent=2) + "\n")
    print(json.dumps({"report": str(base.with_suffix('.md')), "corrections": str(corrections),
                      "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
