#!/usr/bin/env python3
"""Read back every approved Jupiter import and compare it with the provenance ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.apply_goliath_to_jupiter_import import sha256_file


def verify(plan: dict, ledger: dict, root: Path) -> dict:
    expected = [item for item in plan["operations"]
                if item["action"] == "proposed_copy_to_jupiter"]
    by_source: dict[str, list[tuple[Path, dict]]] = {}
    for target_text, record in ledger.items():
        by_source.setdefault(record.get("remote_source", ""), []).append((Path(target_text), record))
    failures = []
    relocated = []
    verified_bytes = 0
    for number, item in enumerate(expected, 1):
        matches = by_source.get(item["remote_source"], [])
        if len(matches) != 1:
            failures.append(f"{item['remote_source']}: {len(matches)} ledger entries")
            continue
        target, record = matches[0]
        if not target.resolve().is_relative_to(root.resolve()):
            failures.append(f"{item['remote_source']}: ledger target escapes Jupiter")
            continue
        if (record.get("asin") != item["asin"] or
                record.get("source_fingerprint") != item["source_fingerprint"] or
                record.get("size") != item["source_bytes"]):
            failures.append(f"{item['remote_source']}: ledger identity differs from plan")
            continue
        if not target.is_file() or target.stat().st_size != item["source_bytes"]:
            failures.append(f"{item['remote_source']}: Jupiter file missing or wrong size")
            continue
        if sha256_file(target) != record.get("sha256"):
            failures.append(f"{item['remote_source']}: Jupiter SHA-256 mismatch")
            continue
        verified_bytes += item["source_bytes"]
        if str(target) != item["jupiter_destination"]:
            relocated.append({"asin": item["asin"], "from_plan": item["jupiter_destination"],
                              "current": str(target)})
        if number % 10 == 0 or number == len(expected):
            print(f"Verified {number}/{len(expected)} planned books", flush=True)
    return {"expected_books": len(expected), "verified_books": len(expected) - len(failures),
            "verified_bytes": verified_bytes, "relocated": relocated, "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--jupiter-root", type=Path, required=True)
    args = parser.parse_args()
    report = verify(json.loads(args.plan.read_text()), json.loads(args.ledger.read_text()),
                    args.jupiter_root.resolve())
    print(json.dumps(report, indent=2))
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
