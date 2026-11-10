"""Whether a retained Task operation still holds an unresolved effect."""

from . import records


def unresolved(operation: dict) -> bool:
    return records.pending(operation) or (operation.get("result") or {}).get("status") == "partial"
