"""Preflight omitted archive books and extensionless audio without modifying either library."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.output import plan_output
from audiobook_manager.probe import probe_media


def archive_book(record: dict[str, Any], output: Path) -> dict[str, Any]:
    archive = Path(record["path"])
    probes = []
    cover: dict[str, Any] | None = None
    with zipfile.ZipFile(archive) as zipped, tempfile.TemporaryDirectory(prefix="audiobook-import-preflight-") as temporary:
        for index, member in enumerate(record["members"]):
            if member["encrypted"]:
                raise ValueError(f"encrypted member: {member['name']}")
            # A generated local filename avoids trusting archive extraction paths.
            local = Path(temporary) / f"track-{index:04d}{Path(member['name']).suffix.lower()}"
            with zipped.open(member["name"]) as source, local.open("xb") as target:
                shutil.copyfileobj(source, target)
            probe = probe_media(local)
            local.unlink()
            if not probe.codec_name or not probe.duration_seconds:
                raise ValueError(f"no readable audio: {member['name']}")
            probes.append({"name": member["name"], "crc32": member["crc32"],
                           "size": member["size"], "duration_seconds": probe.duration_seconds,
                           "codec": probe.codec_name, "tags": probe.tags})
        images = [item for item in zipped.infolist() if not item.is_dir()
                  and Path(item.filename).suffix.casefold() in {".jpg", ".jpeg"}]
        if len(images) == 1 and 0 < images[0].file_size <= 10_000_000:
            with zipped.open(images[0]) as reader:
                content = reader.read(10_000_001)
            if content.startswith(b"\xff\xd8\xff") and len(content) <= 10_000_000:
                cover = {"name": images[0].filename, "crc32": images[0].CRC,
                         "size": images[0].file_size}
    authors = {x["tags"].get("album_artist") or x["tags"].get("artist") for x in probes}
    albums = {x["tags"].get("album") for x in probes}
    if len(authors) != 1 or None in authors or len(albums) != 1 or None in albums:
        raise ValueError("archive does not have one consistent embedded author and album")
    author, album = next(iter(authors)), next(iter(albums))
    match = re.fullmatch(r"(.+?)\s+(\d+(?:\.\d+)?)\s*[-–]\s*(.+)", album)
    if not match:
        raise ValueError(f"album needs manual title/series interpretation: {album}")
    series, number, title = match.groups()
    numbers = []
    for item in probes:
        raw = item["tags"].get("track", "").split("/")[0]
        if not raw.isdigit():
            raise ValueError(f"member has no numeric track tag: {item['name']}")
        numbers.append(int(raw))
    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        raise ValueError("archive track tags are not a complete unique 1..N sequence")
    ordered = [item for _, item in sorted(zip(numbers, probes))]
    metadata = {"authors": [author], "title": title, "series": series,
                "series_position": number, "_metadata_source": "verified_archive_tags"}
    publishers = {item["tags"].get("publisher") for item in probes}
    if len(publishers) == 1 and None not in publishers and "" not in publishers:
        metadata["publisher"] = next(iter(publishers))
    narrator_matches = [re.fullmatch(r"Read by\s+(.+)", item["tags"].get("comment", ""), re.I)
                        for item in probes]
    if all(narrator_matches) and len({match.group(1).strip() for match in narrator_matches if match}) == 1:
        metadata["narrator"] = next(match.group(1).strip() for match in narrator_matches if match)
    target = plan_output(output, metadata)
    destination = target.audio
    if destination.exists():
        raise ValueError(f"target already exists: {destination}")
    if cover and target.cover.exists():
        raise ValueError(f"cover target already exists: {target.cover}")
    return {"action": "extract_to_separate_staging_then_build_m4b",
            "archive": str(archive), "size": record["size"], "mtime_ns": record["mtime_ns"],
            "metadata": metadata, "members_in_order": ordered,
            "expected_duration_seconds": sum(x["duration_seconds"] for x in probes),
            "output": str(destination),
            "cover": cover, "cover_output": str(target.cover) if cover else None,
            "evidence": "every member passes ZIP CRC and audio probe; consistent embedded author/album and complete track sequence"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--completeness", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(args.completeness.read_text())
    output = args.output_root.resolve()
    records = [x for x in audit["archives"] if Path(x["path"]).suffix.lower() == ".zab"
               and x["audio_members"] and x["members_without_unpacked_candidate"] == x["audio_members"]]

    def inspect(record: dict[str, Any]) -> dict[str, Any]:
        try:
            result = archive_book(record, output)
            print(f"Verified {result['metadata']['title']}: {len(result['members_in_order'])} tracks", flush=True)
            return result
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
            return {"action": "review", "source": record["path"], "reason": str(error)}

    with ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(inspect, records))
    for item in audit["suspicious_files"]:
        p = item.get("probe") or {}
        if not p.get("codec_name") or not p.get("duration_seconds"):
            continue
        tags = p.get("tags") or {}
        if not tags.get("title") or not (tags.get("album_artist") or tags.get("artist")):
            records.append({"action": "review", "source": item["path"], "reason": "missing embedded identity"})
            continue
        source = Path(audit["source_root"]) / item["path"]
        metadata = {"authors": [tags.get("album_artist") or tags["artist"]], "title": tags["title"],
                    "_metadata_source": "verified_source_tags"}
        target = plan_output(output, metadata).audio
        records.append({"action": "remux_extensionless_aac_preserving_chapters"
                        if p["codec_name"] == "aac" and not target.exists() else "review",
                        "source": str(source), "size": source.stat().st_size,
                        "mtime_ns": source.stat().st_mtime_ns, "metadata": metadata,
                        "expected_duration_seconds": p["duration_seconds"],
                        "expected_chapters": len(p.get("chapters") or []), "output": str(target)})
    counts = Counter(x["action"] for x in records)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    base = Path("reports") / f"missing-source-import-dry-run-{stamp}"
    report = {"mode": "dry-run", "actions_applied": 0, "based_on": str(args.completeness),
              "source_root": audit["source_root"], "output_root": str(output),
              "expected_run_id": audit["run_id"], "summary": dict(counts), "operations": records,
              "required_validation": ["recheck source fingerprints and all destination collisions",
                  "register archive-member provenance so a subsequent run can recognize the imported book",
                  "stage outside the source library", "verify output duration, chapters and embedded identity",
                  "never overwrite existing output; retain source archives"]}
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Missing-source import dry run", "", "No library media has been changed.", "",
             "Archive members were read in temporary staging and CRC-checked, then probed. Output conversion and registration have not run.", "",
             "| Author | Title | Action | Hours |", "|---|---|---|---:|"]
    for entry in records:
        meta = entry.get("metadata") or {}
        lines.append(f"| {', '.join(meta.get('authors') or [])} | {meta.get('title', entry.get('source'))} | {entry['action']} | {entry.get('expected_duration_seconds', 0)/3600:.2f} |")
    lines += ["", f"Archive books with verified JPEG covers: **{sum(bool(x.get('cover')) for x in records)}**", "",
              "## Required execution checks", "", *[f"- {x}" for x in report["required_validation"]]]
    base.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"summary": dict(counts), "report": str(base.with_suffix('.md'))}, indent=2))


if __name__ == "__main__":
    main()
