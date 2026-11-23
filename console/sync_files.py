"""Per-share file lists recorded after each synchronization job that changed something."""
import logging

import documents
from azure_gateway import AzureApiError
from source_connections import SourcePrerequisiteError

FINISHED = ("success", "transientFailure")


def changed_files(previous, files, processed):
    if previous is not None:
        before = {item["path"]: item.get("modified") for item in previous}
        return sorted(item["path"] for item in files if before.get(item["path"], ...) != item.get("modified"))
    # Without an earlier listing, the indexer's new or modified files are the most recently modified ones.
    recent = sorted(files, key=lambda item: item.get("modified") or "", reverse=True)[:processed]
    return sorted(item["path"] for item in recent)


def record(gateway, store):
    """List shares whose latest indexer run processed files (or that have no listing yet) and store the result."""
    sources = gateway.sources()
    snapshots = store.read().get("syncFiles", {})
    updates = {}
    for row in sources["indexers"]:
        if not row["active"] or row["status"] not in FINISHED or not row.get("endTime"):
            continue
        previous = snapshots.get(row["sourceKey"])
        processed = row.get("itemsProcessed") or 0
        if previous and previous["endTime"] == row["endTime"]:
            continue
        try:
            files = [{"path": item["path"], "modified": item.get("modified")} for item in documents.source_files(row)]
        except (AzureApiError, SourcePrerequisiteError):
            logging.warning("Sync file listing unavailable for one share.")
            continue
        # Runs can follow each other faster than this check: an empty run still refreshes the file list
        # so files indexed by a run that was not observed are counted.
        updates[row["sourceKey"]] = {
            "endTime": row["endTime"], "files": files,
            "changed": changed_files(previous and previous["files"], files, processed) if processed or not previous else [],
        }
    if updates:
        def save(state):
            state.setdefault("syncFiles", {}).update(updates)
        store.update(save)
    return updates
