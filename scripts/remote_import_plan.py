"""Version and validate remote-to-local plans before any execution or verification."""

from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
COPY_ACTION = "proposed_copy_to_destination"


def validate_plan(plan: dict[str, Any], root: Path) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported import plan version; regenerate and review the dry-run plan")
    if plan.get("mode") != "dry_run_no_media_writes":
        raise ValueError("expected a reviewed dry-run import plan")
    destination = plan.get("destination_root")
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        raise ValueError("import plan must specify an absolute destination root")
    if Path(destination).resolve() != root.resolve():
        raise ValueError("destination root does not match the approved plan")
    if not root.is_dir() or plan.get("destination_device") != root.stat().st_dev:
        raise ValueError("destination filesystem changed or is unavailable; regenerate the plan")
    reserve = (plan.get("summary") or {}).get("reserve_bytes")
    if type(reserve) is not int or reserve < 0:
        raise ValueError("approved plan has an invalid destination reserve")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise ValueError("import plan operations must be a list")
    for item in operations:
        if not isinstance(item, dict):
            raise ValueError("import operation must be an object")
        action = item.get("action")
        if not isinstance(action, str) or not (
            action == COPY_ACTION or action.startswith(("review_", "defer_"))
        ):
            raise ValueError("unknown import action; regenerate and review the plan")
