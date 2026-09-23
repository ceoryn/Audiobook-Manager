#!/usr/bin/env python3
"""Read-only reconciliation of an SSH audiobook library with local source/output.

No remote or media-library files are written. Reports are emitted only beneath
the caller-selected report directory. A title match is not proof of equal audio.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.detection import detect_books
from audiobook_manager.models import ProbeResult, ScannedFile


AUDIO = {".mp3", ".m4a", ".m4b", ".flac", ".wav", ".ogg"}

REMOTE_INVENTORY = r"""
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
audio = {'.mp3', '.m4a', '.m4b', '.flac', '.wav', '.ogg'}
if not root.is_dir():
    raise SystemExit('remote audiobook root does not exist')
for path in root.rglob('*'):
    if path.is_file() and path.suffix.casefold() in audio:
        stat = path.stat()
        print(json.dumps({'path': path.relative_to(root).as_posix(),
                          'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns},
                         ensure_ascii=False))
"""

REMOTE_PROBE = r"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
root = Path(sys.argv[1])
paths = json.load(sys.stdin)
def probe(relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        return {'path': relative, 'error': 'path escapes remote root'}
    try:
        proc = subprocess.run(['ffprobe', '-v', 'error', '-show_format',
                               '-show_streams', '-of', 'json', str(path)],
                              capture_output=True, text=True, timeout=45)
        if proc.returncode:
            return {'path': relative, 'error': proc.stderr.strip()[-500:]}
        data = json.loads(proc.stdout)
        fmt = data.get('format') or {}
        stream = next((s for s in data.get('streams', [])
                       if s.get('codec_type') == 'audio'), {})
        tags = {str(k).casefold(): str(v) for k, v in
                {**stream.get('tags', {}), **fmt.get('tags', {})}.items()}
        duration = fmt.get('duration') or stream.get('duration')
        return {'path': relative, 'duration_seconds': float(duration) if duration else None,
                'format_name': fmt.get('format_name'), 'codec_name': stream.get('codec_name'),
                'bitrate': int(fmt['bit_rate']) if fmt.get('bit_rate') else None,
                'sample_rate': int(stream['sample_rate']) if stream.get('sample_rate') else None,
                'channels': stream.get('channels'), 'tags': tags, 'error': None}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'path': relative, 'error': str(error)}
with ThreadPoolExecutor(max_workers=8) as pool:
    for item in pool.map(probe, paths):
        print(json.dumps(item, ensure_ascii=False))
"""


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def title_aliases(value: object) -> set[str]:
    name = normalized(value)
    aliases = {name} if name else set()
    aliases.add(re.sub(r"^(?:star wars|the) ", "", name))
    aliases.add(re.sub(r"\b(?:unabridged|graphic audio|dramatized adaptation)\b", "", name).strip())
    return {item for item in aliases if len(item) >= 3}


def remote_command(args: argparse.Namespace, program: str, *, input_text: str | None = None) -> str:
    ssh = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10"]
    if args.control_socket:
        ssh += ["-S", str(args.control_socket)]
    if args.known_hosts:
        ssh += ["-o", f"UserKnownHostsFile={args.known_hosts}"]
    ssh += [args.host, "python3 -c " + shlex.quote(program) + " " +
            shlex.quote(args.remote_root)]
    result = subprocess.run(ssh, input=input_text, capture_output=True, text=True,
                            timeout=300)
    if result.returncode:
        raise RuntimeError(f"remote read failed: {result.stderr.strip()[-1000:]}")
    return result.stdout


def local_inventory(root: Path) -> dict[str, dict[str, int]]:
    records: dict[str, dict[str, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in AUDIO:
            continue
        stat = path.stat()
        records[path.relative_to(root).as_posix()] = {
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return records


def active_outputs(root: Path) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for path in root.rglob("*.m4b"):
        if "_quarantine" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root).as_posix()
        name = path.stem.split(" - ", 1)[-1]
        titles = title_aliases(name) | title_aliases(path.parent.name)
        outputs.append({"path": relative, "titles": sorted(titles),
                        "author": normalized(relative.split("/", 1)[0])})
    return outputs


def name_for(group: Any) -> tuple[str, str]:
    author, _, key_title = group.key.partition(" — ")
    first = group.files[0]
    tags = first.probe.tags if first.probe else {}
    path = first.relative_path
    title = tags.get("album") or tags.get("title") or key_title or path.parent.name
    if normalized(title) in {"album", "untitled", "unknown", "book"}:
        title = path.parent.name
    if path.parts[0] == "Books" and len(path.parts) > 2:
        title = re.sub(r" \[[^]]+\]$", "", path.parts[1])
    byline = tags.get("album_artist") or tags.get("artist") or author
    return str(title), str(byline)


def classify(title: str, author: str, outputs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    aliases = title_aliases(title)
    if not aliases:
        return "review_identity", []
    author_key = normalized(author)
    exact = [item for item in outputs if aliases.intersection(item["titles"])]
    if exact:
        same_author = [item for item in exact if author_key and item["author"] and (
            item["author"] == author_key or author_key in item["author"] or
            item["author"] in author_key)]
        if same_author:
            return "represented_title", [item["path"] for item in same_author[:5]]
        return "review_author_or_edition", [item["path"] for item in exact[:5]]
    path_hits = [item for item in outputs if author_key and item["author"] and (
        item["author"] == author_key or author_key in item["author"] or
        item["author"] in author_key) and any(
        len(alias) >= 10 and alias in normalized(item["path"])
        for alias in aliases)]
    if path_hits:
        return "review_possible_alias", [item["path"] for item in path_hits[:5]]
    return "missing_candidate", []


def build(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source_root).resolve()
    output = Path(args.output_root).resolve()
    if not source.is_dir() or not output.is_dir():
        raise ValueError("local source and output roots must exist")
    remote = {row["path"]: row for row in
              (json.loads(line) for line in remote_command(args, REMOTE_INVENTORY).splitlines())}
    local = local_inventory(source)
    shared_paths = set(remote) & set(local)
    changed_size = sorted(path for path in shared_paths
                          if remote[path]["size"] != local[path]["size"])
    remote_only = sorted(set(remote) - set(local))
    temp = [path for path in remote_only if path.split("/", 1)[0].casefold().endswith("-tmpfiles")]
    temp_paths = set(temp)
    candidate_paths = [path for path in remote_only if path not in temp_paths]
    probes = {row["path"]: row for row in (json.loads(line) for line in
              remote_command(args, REMOTE_PROBE, input_text=json.dumps(candidate_paths)).splitlines())}
    scanned: list[ScannedFile] = []
    for relative in candidate_paths:
        record = remote[relative]
        info = probes[relative]
        probe = None if info.get("error") else ProbeResult(
            duration_seconds=info.get("duration_seconds"),
            format_name=info.get("format_name"), codec_name=info.get("codec_name"),
            bitrate=info.get("bitrate"), sample_rate=info.get("sample_rate"),
            channels=info.get("channels"), tags=info.get("tags", {}))
        scanned.append(ScannedFile(Path(args.remote_root) / relative,
                                   Path(relative), record["size"], record["mtime_ns"],
                                   probe, info.get("error")))
    outputs = active_outputs(output)
    books: list[dict[str, Any]] = []
    for group in detect_books(scanned):
        title, author = name_for(group)
        classification, matches = classify(title, author, outputs)
        books.append({"book_key": group.key, "title": title, "author": author,
                      "classification": classification,
                      "source_files": [file.relative_path.as_posix() for file in group.files],
                      "source_fingerprints": [
                          {"path": file.relative_path.as_posix(),
                           "size": remote[file.relative_path.as_posix()]["size"],
                           "mtime_ns": remote[file.relative_path.as_posix()]["mtime_ns"]}
                          for file in group.files],
                      "source_bytes": sum(file.size for file in group.files),
                      "duration_seconds": sum((file.probe.duration_seconds or 0)
                                              for file in group.files if file.probe),
                      "matched_outputs": matches,
                      "probe_errors": [file.relative_path.as_posix() for file in group.files
                                       if file.error]})
    books.sort(key=lambda item: (item["classification"], item["author"].casefold(),
                                 item["title"].casefold()))
    classification_counts = Counter(item["classification"] for item in books)
    bytes_by_classification: Counter[str] = Counter()
    for item in books:
        bytes_by_classification[item["classification"]] += item["source_bytes"]
    return {"mode": "read_only_dry_run", "generated_at": datetime.now().astimezone().isoformat(),
            "remote_host": args.host, "remote_root": args.remote_root,
            "local_source_root": str(source), "output_root": str(output),
            "summary": {"remote_audio_files": len(remote), "local_source_audio_files": len(local),
                        "same_relative_path": len(set(remote) & set(local)),
                        "same_path_different_size": len(changed_size),
                        "remote_only_audio_paths": len(remote_only),
                        "remote_only_temporary_audio_paths": len(temp),
                        "remote_only_candidate_audio_paths": len(candidate_paths),
                        "candidate_bytes": sum(remote[path]["size"] for path in candidate_paths),
                        "detected_candidate_books": len(books),
                        "book_classifications": dict(classification_counts),
                        "bytes_by_classification": dict(bytes_by_classification),
                        "remote_probe_errors": sum(bool(item.get("error"))
                                                   for item in probes.values())},
            "temporary_paths": temp, "same_path_different_size": changed_size,
            "books": books,
            "limitations": ["Filename and tag matching is not proof of identical audio.",
                            "Equal relative paths and sizes are not proof of identical audio.",
                            "Remote-only paths can duplicate books under different source paths.",
                            "A missing candidate must be reviewed before any output write.",
                            "The remote library and local source were not modified."]}


def markdown(report: dict[str, Any]) -> str:
    lines = ["# Goliath audiobook reconciliation", "", report["generated_at"], "",
             "Read-only dry run. No media copied or modified.", "", "## Summary", ""]
    lines += [f"- {key.replace('_', ' ')}: {value}" for key, value in
              report["summary"].items() if key not in {"book_classifications",
                                                       "bytes_by_classification"}]
    lines += [f"- {key.replace('_', ' ')}: {value}" for key, value in
              report["summary"]["book_classifications"].items()]
    for category in ("missing_candidate", "review_identity", "review_possible_alias",
                     "review_author_or_edition", "represented_title"):
        subset = [item for item in report["books"] if item["classification"] == category]
        lines += ["", f"## {category.replace('_', ' ').title()} ({len(subset)})", "",
                  "| Author | Title | Files | GiB | Matched output |",
                  "|---|---|---:|---:|---|"]
        for item in subset:
            author = item["author"].replace("|", "\\|")
            title = item["title"].replace("|", "\\|")
            matched = ", ".join(item["matched_outputs"][:1]).replace("|", "\\|")
            lines.append(f"| {author} | {title} | {len(item['source_files'])} | "
                         f"{item['source_bytes'] / 2**30:.2f} | {matched} |")
    lines += ["", "## Safety", "", *[f"- {item}" for item in report["limitations"]], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH user@host")
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--control-socket", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--reclassify-from", type=Path,
                        help="Reclassify a saved report without probing the remote library again")
    args = parser.parse_args()
    if args.reclassify_from:
        report = json.loads(args.reclassify_from.read_text())
        outputs = active_outputs(args.output_root.resolve())
        for item in report["books"]:
            item["classification"], item["matched_outputs"] = classify(
                item["title"], item["author"], outputs)
        report["generated_at"] = datetime.now().astimezone().isoformat()
        report["reclassified_from"] = str(args.reclassify_from.resolve())
        report["summary"]["book_classifications"] = dict(Counter(
            item["classification"] for item in report["books"]))
        byte_counts: Counter[str] = Counter()
        for item in report["books"]:
            byte_counts[item["classification"]] += item["source_bytes"]
        report["summary"]["bytes_by_classification"] = dict(byte_counts)
    else:
        report = build(args)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    base = args.report_dir / f"remote-library-reconciliation-{stamp}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    base.with_suffix(".md").write_text(markdown(report))
    print(json.dumps({"json": str(base.with_suffix(".json")),
                      "markdown": str(base.with_suffix(".md")),
                      "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
