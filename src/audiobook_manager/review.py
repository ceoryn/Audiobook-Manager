from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ReviewItem:
    relationship_id: str
    evidence_fingerprint: str
    group_key: str
    relationship: dict[str, Any]
    review_reasons: tuple[str, ...]

    @property
    def confidence(self) -> float:
        return float(self.relationship["confidence"])

    @property
    def confidence_band(self) -> str:
        return str(self.relationship["confidence_band"])


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_items(analysis: dict[str, Any]) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    for group in analysis.get("groups", []):
        reasons = tuple(group.get("review_reasons", ()))
        for relationship in group.get("relationships", []):
            identity = {
                "group_key": group["group_key"],
                "relationship": relationship["relationship"],
                "source_files": relationship.get("source_files", []),
                "target_file": relationship.get("target_file"),
            }
            evidence_state = {
                **identity,
                "confidence": relationship.get("confidence"),
                "evidence": relationship.get("evidence", []),
                "conflicts": relationship.get("conflicts", []),
            }
            items.append(
                ReviewItem(
                    relationship_id=_digest(identity)[:16],
                    evidence_fingerprint=_digest(evidence_state),
                    group_key=group["group_key"],
                    relationship=relationship,
                    review_reasons=reasons,
                )
            )
    return sorted(items, key=lambda item: (-item.confidence, item.group_key.casefold(), item.relationship_id))


def filter_items(
    items: Iterable[ReviewItem],
    *,
    band: str | None = None,
    decision: str | None = None,
    decisions: dict[str, dict[str, str | None]] | None = None,
) -> list[ReviewItem]:
    saved = decisions or {}
    result: list[ReviewItem] = []
    for item in items:
        if band and item.confidence_band != band:
            continue
        state = saved.get(item.relationship_id)
        current = state.get("decision") if state else "unreviewed"
        if decision and current != decision:
            continue
        result.append(item)
    return result


def decision_status(
    item: ReviewItem, decisions: dict[str, dict[str, str | None]]
) -> tuple[str, bool]:
    saved = decisions.get(item.relationship_id)
    if not saved:
        return "unreviewed", False
    stale = saved.get("evidence_fingerprint") != item.evidence_fingerprint
    return str(saved["decision"]), stale


def render_queue(
    items: list[ReviewItem],
    decisions: dict[str, dict[str, str | None]],
    *,
    limit: int,
) -> str:
    lines = [f"Review queue: showing {min(limit, len(items))} of {len(items)} relationships", ""]
    for item in items[:limit]:
        status, stale = decision_status(item, decisions)
        marker = f"{status}{' (evidence changed)' if stale else ''}"
        target = item.relationship.get("target_file") or "component set"
        lines.extend(
            [
                f"[{item.relationship_id}] {item.confidence:5.1f} {item.confidence_band.upper():6} {marker}",
                f"  {item.relationship['relationship']}: {item.group_key}",
                f"  target: {target}",
                f"  sources: {len(item.relationship.get('source_files', []))}; conflicts: {len(item.relationship.get('conflicts', []))}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_detail(item: ReviewItem, decisions: dict[str, dict[str, str | None]]) -> str:
    relationship = item.relationship
    status, stale = decision_status(item, decisions)
    lines = [
        f"Relationship {item.relationship_id}",
        f"Group: {item.group_key}",
        f"Type: {relationship['relationship']}",
        f"Confidence: {item.confidence:.1f} ({item.confidence_band})",
        f"Decision: {status}{' (evidence changed; review again)' if stale else ''}",
        f"Target: {relationship.get('target_file') or 'component set'}",
        "Sources:",
    ]
    lines.extend(f"  - {path}" for path in relationship.get("source_files", []))
    lines.append("Evidence:")
    lines.extend(
        f"  + {entry['points']:g}: {entry['explanation']}" for entry in relationship.get("evidence", [])
    )
    lines.append("Conflicts:")
    conflicts = relationship.get("conflicts", [])
    lines.extend(f"  - {conflict}" for conflict in conflicts)
    if not conflicts:
        lines.append("  - none recorded")
    if item.review_reasons:
        lines.append("Review reasons:")
        lines.extend(f"  - {reason}" for reason in item.review_reasons)
    lines.extend(["Safety: analysis only; this decision cannot modify or clean up media files.", ""])
    return "\n".join(lines)
