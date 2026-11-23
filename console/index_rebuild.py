"""Purge of chunks of files deleted from Azure Files.

Incremental indexer runs never remove chunks of deleted source files. The default
"targeted" purge reads the chunk inventory of the active index (elevated read),
lists the connected shares and deletes only the chunks whose file no longer exists
or whose share is no longer connected. Source files are never touched.

The "rebuild" mode (and the fallback when the identity lacks the delete or elevated
read permission) is a blue/green rebuild: it recreates the inactive slot as an empty
index, fully reindexes connected shares into it, validates it, points the knowledge
source at it and moves the incremental schedules. The previous index is kept
unqueried as a rollback target until the next rebuild recreates it.
"""

import copy
import logging
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from azure_gateway import AzureApiError, editable, group_from_skillset
from state import StateConflict, now


TIMEZONE = ZoneInfo("Europe/Paris")
INTERVALS = (1, 2, 3, 4, 6, 8, 12, 24)
SHARES = ("admin", "alluser")
DATA_SOURCES = {share: f"af-ds-{share}" for share in SHARES}
INDEXES = ("af-index-all", "af-index-all-b")
PHASES = ("prepare", "idle", "recreate", "slot", "reset", "run", "wait", "stats", "switch", "schedule")
DEFAULT_SCHEDULE = {
    "enabled": False, "intervalHours": 24, "startHour": 2,
    "lastSlot": None, "updatedAt": None, "updatedBy": None,
}
STATS_TIMEOUT_SECONDS = 420
MODES = ("targeted", "rebuild")
KEY_FIELD = "chunk_id"
PAGE_SIZE = 1000
ELEVATED_READ = {"x-ms-enable-elevated-read": "true"}


def slot(index):
    if index not in INDEXES:
        raise AzureApiError("The active index is outside the configured application slots.")
    suffix = "" if index == "af-index-all" else "-b"
    return {
        "index": index,
        "indexers": {share: f"af-indexer-all{suffix}-{share}" for share in SHARES},
        "skillsets": {share: f"af-skills-all{suffix}-{share}" for share in SHARES},
    }


def other(index):
    return INDEXES[1] if index == INDEXES[0] else INDEXES[0]


def settings_from(state):
    return {**DEFAULT_SCHEDULE, **(state.get("rebuild") or {})}


def validate_schedule(enabled, interval_hours, start_hour):
    if not isinstance(enabled, bool):
        raise ValueError("Enabled must be true or false.")
    if isinstance(interval_hours, bool) or interval_hours not in INTERVALS:
        raise ValueError("Choose a supported rebuild interval.")
    if isinstance(start_hour, bool) or not isinstance(start_hour, int) or not 0 <= start_hour <= 23:
        raise ValueError("Choose a start hour between 0 and 23.")


def _slots_around(settings, moment):
    """Scheduled instants (UTC) on the local days around moment.

    Slots are wall-clock hours in Europe/Paris. A nonexistent spring-forward
    hour resolves to the next real instant; a repeated autumn hour runs once.
    """
    local_day = moment.astimezone(TIMEZONE).date()
    hours = [hour for hour in range(24) if (hour - settings["startHour"]) % settings["intervalHours"] == 0]
    return sorted({
        datetime.combine(local_day + timedelta(days=offset), time(hour), TIMEZONE).astimezone(timezone.utc)
        for offset in (-1, 0, 1) for hour in hours
    })


def due_slot(settings, moment):
    past = [value for value in _slots_around(settings, moment) if value <= moment]
    return past[-1] if past else None


def next_slot(settings, moment):
    return next(value for value in _slots_around(settings, moment) if value > moment)


def initial_phase(operation):
    return "purge" if operation.get("mode") == "targeted" else "prepare"


def new_operation(operation_id, actor, trigger, mode="targeted"):
    if mode not in MODES:
        raise ValueError("Choose a supported purge mode.")
    operation = {
        "id": operation_id, "type": "rebuild", "trigger": trigger, "mode": mode, "status": "queued",
        "actor": actor, "createdAt": now(), "updatedAt": now(), "cursor": 0, "switched": False,
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat(),
    }
    operation["phase"] = initial_phase(operation)
    return operation


def layouts(sources):
    from source_connections import names

    active = sources["activeIndex"]
    target_index = other(active)
    current = {"index": active, "indexers": {}, "skillsets": {}}
    target = {"index": target_index, "indexers": {}, "skillsets": {}}
    data_sources = {}
    for row in sources["indexers"]:
        if not row["active"]:
            continue
        key = next((key for key, value in DATA_SOURCES.items() if value == row["source"]), row["source"])
        data_sources[key] = row["source"]
        current["indexers"][key], current["skillsets"][key] = row["name"], row["skillset"]
        peers = [peer for peer in sources["indexers"] if peer["source"] == row["source"] and peer["index"] == target_index]
        if len(peers) > 1:
            raise AzureApiError("A source has ambiguous inactive-slot indexers.")
        if peers:
            target["indexers"][key], target["skillsets"][key] = peers[0]["name"], peers[0]["skillset"]
        elif key in SHARES:
            legacy = slot(target_index)
            target["indexers"][key], target["skillsets"][key] = legacy["indexers"][key], legacy["skillsets"][key]
        else:
            generated = names(row["account"], row["share"], target_index)
            target["indexers"][key], target["skillsets"][key] = generated["name"], generated["skillset"]
    return current, target, data_sources


def _set_schedule(gateway, name, interval):
    path = f"/indexers/{quote(name, safe='')}"
    current = gateway.search("GET", path, expected=(200, 404))
    if current is None or (current.get("schedule") or {}).get("interval") == interval:
        return
    body = editable(current)
    if interval:
        body["schedule"] = {"interval": interval}
    else:
        body.pop("schedule", None)
    gateway.search("PUT", path, body, {"If-Match": current["@odata.etag"]}, (200, 201, 204))


def _last(gateway, name):
    return gateway.search("GET", f"/indexers/{quote(name, safe='')}/status").get("lastResult") or {}


def _count(gateway, index):
    value = gateway.search("GET", f"/indexes/{index}/stats").get("documentCount")
    if not isinstance(value, int) or value < 0:
        raise AzureApiError("Azure did not return valid index statistics.")
    return value


def _skillset_copy(source, name, target_index):
    body = editable(source)
    body["name"] = name
    for skill in body.get("skills", []):
        if skill.get("@odata.type") == "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill":
            skill.pop("apiKey", None)
            skill.pop("authIdentity", None)
    for selector in body.get("indexProjections", {}).get("selectors", []):
        selector["targetIndexName"] = target_index
    return body


def previous_source_paths(operations, operation_id):
    previous = next((op for op in operations if op["type"] == "rebuild" and op["id"] != operation_id
                     and op.get("status") == "completed" and op.get("sourcePaths")), None)
    return previous["sourcePaths"] if previous else None


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def job_counters(sources, operations, snapshots=None):
    """Attach per-share counters recorded by the last jobs: sync listings, Azure's last sync result and the last purge."""
    last = next((op for op in operations if op["type"] == "rebuild" and op.get("status") == "completed"), None)
    snapshots = snapshots or {}
    result = copy.deepcopy(sources)
    for row in result["indexers"]:
        purge = {"at": None, "indexedCount": None, "purgedCount": None}
        if last:
            purge["at"] = last.get("switchedAt") or last.get("purgedAt") or last.get("updatedAt")
            key = next((name for name, value in (last.get("dataSources") or DATA_SOURCES).items()
                        if value == row["source"]), None)
            processed = (last.get("itemsProcessed") or {}).get(key)
            if isinstance(processed, int) and processed >= 0:
                purge["indexedCount"] = processed
        purged = _all_purged(operations, row["sourceKey"])
        if purged is not None:
            purge["purgedCount"] = len(purged)
        row["lastPurge"] = purge
        if not row["active"]:
            _standby_counters(row, operations)
            continue
        snapshot = snapshots.get(row["sourceKey"])
        indexed = {"count": purge["indexedCount"], "at": purge["at"], "from": "purge" if purge["at"] else None,
                   "listed": bool(last and (last.get("sourcePaths") or {}).get(row["sourceKey"]))}
        if snapshot and (not purge["at"] or _instant(snapshot["endTime"]) > _instant(purge["at"])):
            indexed = {"count": len(snapshot["files"]), "at": snapshot["endTime"], "from": "sync", "listed": True}
        row["indexed"] = indexed
        # A refresh that processed nothing has a known, empty list even though no snapshot is taken.
        listed = bool(snapshot and row.get("endTime") and snapshot["endTime"] == row.get("endTime")
                      and isinstance(snapshot.get("changed"), list))
        row["lastSyncListed"] = bool(row.get("endTime") and (row.get("itemsProcessed") == 0 or listed))
        # Azure's itemsProcessed can include unchanged files it re-read; the count shown is the recorded list.
        row["lastSyncCount"] = len(snapshot["changed"]) if listed else 0 if row["lastSyncListed"] else None
    return result


def _slot_build(operations, index):
    """The last completed purge that built the given index slot."""
    return next((op for op in operations if op["type"] == "rebuild" and op.get("status") == "completed"
                 and op.get("targetIndex") == index), None)


def _standby_counters(row, operations):
    """Counters of a standby (inactive) indexer: the purge that built its slot and Azure's last result."""
    build = _slot_build(operations, row["index"])
    indexed = {"count": None, "at": None, "from": None, "listed": False}
    if build:
        key = next((name for name, value in (build.get("dataSources") or DATA_SOURCES).items()
                    if value == row["source"]), None)
        processed = (build.get("itemsProcessed") or {}).get(key)
        indexed = {"count": processed if isinstance(processed, int) and processed >= 0 else None,
                   "at": build.get("switchedAt") or build.get("updatedAt"), "from": "purge",
                   "listed": bool((build.get("sourcePaths") or {}).get(row["sourceKey"]))}
    row["indexed"] = indexed
    row["lastSyncListed"] = False
    processed = row.get("itemsProcessed")
    row["lastSyncCount"] = processed if isinstance(processed, int) and processed >= 0 else None


def _all_purged(operations, source_key):
    """Every file purged for one share by all completed purges, newest first; None if no purge recorded a list."""
    lists = [op["purgedFiles"] for op in operations
             if op["type"] == "rebuild" and op.get("status") == "completed" and isinstance(op.get("purgedFiles"), list)]
    if not lists:
        return None
    return [item["path"] for files in lists for item in files if item.get("sourceKey") == source_key]


def job_files(sources, operations, snapshots, source_key, kind, standby=False):
    """Return the file paths behind one counter, from the same job records as job_counters."""
    row = next((item for item in job_counters(sources, operations, snapshots)["indexers"]
                if item["active"] != standby and item["sourceKey"] == source_key), None)
    if not row:
        raise ValueError("Choose a connected share.")
    if standby:
        if kind == "indexed" and row["indexed"]["listed"]:
            return _slot_build(operations, row["index"])["sourcePaths"][source_key]["paths"], row["indexed"]["at"]
        if kind == "purged" and row["lastPurge"]["purgedCount"] is not None:
            return _all_purged(operations, source_key), row["lastPurge"]["at"]
        if kind not in ("lastSync", "indexed", "purged"):
            raise ValueError("Choose a supported file list.")
        return None, None
    last = next((op for op in operations if op["type"] == "rebuild" and op.get("status") == "completed"), None)
    snapshot = snapshots.get(source_key)
    if kind == "lastSync" and row["lastSyncListed"]:
        if snapshot and snapshot["endTime"] == row["endTime"] and isinstance(snapshot.get("changed"), list):
            return snapshot["changed"], row["endTime"]
        return [], row["endTime"]
    if kind == "indexed" and row["indexed"]["listed"]:
        if row["indexed"]["from"] == "sync":
            return [item["path"] for item in snapshot["files"]], row["indexed"]["at"]
        return last["sourcePaths"][source_key]["paths"], row["indexed"]["at"]
    if kind == "purged" and row["lastPurge"]["purgedCount"] is not None:
        return _all_purged(operations, source_key), row["lastPurge"]["at"]
    if kind not in ("lastSync", "indexed", "purged"):
        raise ValueError("Choose a supported file list.")
    return None, None


def _index_chunks(gateway, index):
    """Key and source file URL of every chunk; elevated read bypasses the per-group permission filter."""
    chunks, last = [], None
    while True:
        body = {"search": "*", "select": f"{KEY_FIELD},metadata_storage_path", "orderby": f"{KEY_FIELD} asc", "top": PAGE_SIZE}
        if last is not None:
            body["filter"] = f"{KEY_FIELD} gt '{last.replace(chr(39), chr(39) * 2)}'"
        page = (gateway.search("POST", f"/indexes/{quote(index, safe='')}/docs/search", body, ELEVATED_READ) or {}).get("value") or []
        chunks += [(item[KEY_FIELD], item.get("metadata_storage_path")) for item in page]
        if len(page) < PAGE_SIZE:
            return chunks
        if page[-1][KEY_FIELD] == last:
            raise AzureApiError("The index inventory did not advance. Nothing was purged.")
        last = page[-1][KEY_FIELD]


def _chunk_file(url):
    """(account, share, path) of an Azure Files URL, or None when it cannot be parsed safely."""
    try:
        parts = urlsplit(url or "")
        labels = (parts.hostname or "").lower().split(".")
    except ValueError:
        return None
    if parts.scheme != "https" or len(labels) < 3 or labels[1] != "file" or not labels[0]:
        return None
    share, _, path = unquote(parts.path).lstrip("/").partition("/")
    return (labels[0], share.lower(), path) if share and path else None


def _delete_chunks(gateway, index, keys):
    for start in range(0, len(keys), PAGE_SIZE):
        batch = keys[start:start + PAGE_SIZE]
        result = gateway.search("POST", f"/indexes/{quote(index, safe='')}/docs/index",
                                {"value": [{"@search.action": "delete", KEY_FIELD: key} for key in batch]},
                                expected=(200, 207))
        rows = (result or {}).get("value") or []
        if sorted(row.get("key") or "" for row in rows) != sorted(batch) or not all(row.get("status") for row in rows):
            raise AzureApiError("Azure did not confirm every chunk deletion. Resume the purge to finish it.")


def _targeted_purge(gateway):
    from documents import source_files
    from source_connections import SourcePrerequisiteError

    sources = gateway.sources(include_status=False)
    index = sources["activeIndex"]
    # The index is read before the shares are listed, so a file added meanwhile is never purged.
    chunks = _index_chunks(gateway, index)
    rows = [row for row in sources["indexers"] if row["active"]]
    listed = {}
    try:
        for row in rows:
            listed[(row["account"].lower(), row["share"].lower())] = (
                row, {item["path"].casefold() for item in source_files(row)})
    except (AzureApiError, SourcePrerequisiteError) as error:
        raise AzureApiError(f"A share could not be listed, so nothing was purged. {error}") from error
    doomed, gone, kept, unparsed = [], {}, {source: {} for source in listed}, 0
    for key, url in chunks:
        located = _chunk_file(url)
        if located is None:
            unparsed += 1
            continue
        account, share, path = located
        entry = listed.get((account, share))
        if entry and path.casefold() in entry[1]:
            kept[(account, share)][path.casefold()] = path
            continue
        doomed.append(key)
        gone[(account, share, path)] = entry[0] if entry else None
    if unparsed:
        logging.warning("Purge skipped %s chunks with an unrecognized file URL.", unparsed)
    _delete_chunks(gateway, index, doomed)
    data_sources, processed, paths = {}, {}, {}
    for (account, share), (row, _) in listed.items():
        name = next((key for key, value in DATA_SOURCES.items() if value == row["source"]), row["source"])
        data_sources[name] = row["source"]
        processed[name] = len(kept[(account, share)])
        paths[row["sourceKey"]] = {**{field: row[field] for field in ("account", "share", "resourceGroup")},
                                   "paths": sorted(kept[(account, share)].values())}
    purged = sorted(({"sourceKey": row["sourceKey"] if row else f"{account}/{share}", "account": account, "share": share,
                      "resourceGroup": row["resourceGroup"] if row else None, "path": path}
                     for (account, share, path), row in gone.items()), key=lambda item: (item["sourceKey"], item["path"]))
    return {
        "activeIndex": index, "dataSources": data_sources, "sourceNames": list(data_sources),
        "itemsProcessed": processed, "sourcePaths": paths, "purgedFiles": purged, "purgedFileCount": len(purged),
        "deletedChunks": len(doomed), "skippedChunks": unparsed, "previousChunks": len(chunks),
        "newChunks": len(chunks) - len(doomed),
    }


def step(gateway, sanitized_schema, operation, previous_paths=None):
    op = copy.deepcopy(operation)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(op["deadline"]):
        raise StateConflict("The index rebuild timed out. Inspect Azure status and resume explicitly.")
    if op.get("mode") == "targeted":
        if op["phase"] != "purge":
            raise StateConflict("The purge operation contains an unknown phase.")
        try:
            op.update(_targeted_purge(gateway), status="completed", purgedAt=now())
        except AzureApiError as error:
            if error.http_status not in (400, 401, 403):
                raise
            # Without delete or elevated read permission (Search may reject elevated reads with 400 for some
            # identities), rebuilding a clean index is the only way to purge.
            logging.warning("Targeted purge denied by Azure (HTTP %s); falling back to a full rebuild.", error.http_status)
            op.update(mode="rebuild", phase="prepare", cursor=0, status="running",
                      fallbackReason=f"Targeted purge not permitted (HTTP {error.http_status}); full index rebuild used.")
        op["updatedAt"] = now()
        return op
    phase, cursor = op["phase"], op.get("cursor", 0)
    target = op.get("targetLayout") or (slot(op["targetIndex"]) if op.get("targetIndex") else None)
    shares = op.get("sourceNames", SHARES)
    data_sources = op.get("dataSources", DATA_SOURCES)
    if phase == "prepare":
        sources = gateway.sources()
        active, target, data_sources = layouts(sources)
        intervals = {}
        for row in sources["indexers"]:
            if row["active"]:
                share = next(key for key, value in data_sources.items() if value == row["source"])
                intervals[share] = row["schedule"]
        for name in target["indexers"].values():
            _set_schedule(gateway, name, None)
        op.update(
            status="running", activeIndex=active["index"], targetIndex=target["index"],
            intervals=intervals, phase="idle", cursor=0, activeLayout=active, targetLayout=target,
            sourceNames=list(data_sources), dataSources=data_sources,
        )
    elif phase == "idle":
        for name in target["indexers"].values():
            if gateway.search("GET", f"/indexers/{quote(name, safe='')}", expected=(200, 404)) is not None \
                    and _last(gateway, name).get("status") == "inProgress":
                return op
        op["phase"] = "recreate"
    elif phase == "recreate":
        if gateway.sources()["activeIndex"] != op["activeIndex"]:
            raise StateConflict("The knowledge-source target changed before the rebuild started.")
        schema = sanitized_schema(gateway.search("GET", f"/indexes/{op['activeIndex']}"))
        schema["name"] = target["index"]
        gateway.search("DELETE", f"/indexes/{target['index']}", expected=(204, 404))
        gateway.search("PUT", f"/indexes/{target['index']}", schema, {"If-None-Match": "*"}, (201,))
        if shares:
            op.update(phase="slot", cursor=0)
        else:
            # No source left: switch the agent to the empty index.
            op.update(phase="switch", cursor=0, itemsProcessed={}, newChunks=0,
                      previousChunks=_count(gateway, op["activeIndex"]))
    elif phase == "slot":
        share = shares[cursor]
        active = op.get("activeLayout") or slot(op["activeIndex"])
        template = gateway.search("GET", f"/skillsets/{active['skillsets'][share]}")
        group = group_from_skillset(template)
        name = target["skillsets"][share]
        current = gateway.search("GET", f"/skillsets/{name}", expected=(200, 404))
        if current is None:
            gateway.search("PUT", f"/skillsets/{name}", _skillset_copy(template, name, target["index"]),
                           {"If-None-Match": "*"}, (201,))
        else:
            targets = {item.get("targetIndexName") for item in current.get("indexProjections", {}).get("selectors", [])}
            if targets != {target["index"]}:
                raise AzureApiError(f"Skillset {name} does not project into {target['index']}.")
            if group_from_skillset(current) != group:
                raise StateConflict(f"The {share} source group differs between slots. Review permissions before rebuilding.")
        indexer = target["indexers"][share]
        current = gateway.search("GET", f"/indexers/{indexer}", expected=(200, 404))
        if current is None:
            body = editable(gateway.search("GET", f"/indexers/{active['indexers'][share]}"))
            body.update(name=indexer, targetIndexName=target["index"], skillsetName=name)
            body.pop("schedule", None)
            gateway.search("PUT", f"/indexers/{indexer}", body, {"If-None-Match": "*"}, (201,))
        elif (current.get("targetIndexName"), current.get("skillsetName"), current.get("dataSourceName")) != (
                target["index"], name, data_sources[share]):
            raise AzureApiError(f"Indexer {indexer} has an unexpected configuration.")
        if current is not None and current.get("disabled"):
            body = editable(current)
            body["disabled"] = False
            gateway.search("PUT", f"/indexers/{indexer}", body, {"If-Match": current["@odata.etag"]}, (200, 201, 204))
        op["cursor"] += 1
        if op["cursor"] == len(shares):
            op.update(phase="reset", cursor=0, previousStarts={})
    elif phase == "reset":
        name = target["indexers"][shares[cursor]]
        if _last(gateway, name).get("status") == "inProgress":
            return op
        gateway.search("POST", f"/indexers/{name}/reset", expected=(204,))
        op.setdefault("previousStarts", {})[name] = _last(gateway, name).get("startTime")
        op["cursor"] += 1
        if op["cursor"] == len(shares):
            op.update(phase="run", cursor=0)
    elif phase == "run":
        name = target["indexers"][shares[cursor]]
        result = _last(gateway, name)
        if not result.get("startTime") or result["startTime"] == op["previousStarts"].get(name):
            gateway.search("POST", f"/indexers/{name}/run", expected=(202,))
        op["cursor"] += 1
        if op["cursor"] == len(shares):
            op.update(phase="wait", cursor=0)
    elif phase == "wait":
        results = {}
        for share in shares:
            name = target["indexers"][share]
            result = _last(gateway, name)
            if not result.get("startTime") or result["startTime"] == op["previousStarts"].get(name) \
                    or result.get("status") == "inProgress":
                return op
            if result.get("status") != "success" or result.get("itemsFailed"):
                raise StateConflict(f"Indexer {name} did not complete successfully. The active index was kept.")
            results[share] = result.get("itemsProcessed") or 0
        op.update(phase="stats", itemsProcessed=results, statsStartedAt=now(), observedCount=None)
    elif phase == "stats":
        count = _count(gateway, target["index"])
        if not count or count != op.get("observedCount"):
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(op["statsStartedAt"])
            if elapsed.total_seconds() >= STATS_TIMEOUT_SECONDS:
                raise StateConflict("Rebuilt index statistics did not stabilize with documents. The active index was kept.")
            op["observedCount"] = count
            return op
        previous = _count(gateway, op["activeIndex"])
        sanitized_schema(gateway.search("GET", f"/indexes/{target['index']}"))
        op.update(phase="switch", newChunks=count, previousChunks=previous)
    elif phase == "switch":
        if "sourcePaths" not in op and "inventoryError" not in op:
            # Azure Files is readable app-only; protected index content is not, so purged files are
            # the source paths recorded by the previous purge that no longer exist.
            from documents import source_files
            from source_connections import SourcePrerequisiteError
            try:
                rows = [row for row in gateway.sources(include_status=False)["indexers"] if row["active"]]
                op["sourcePaths"] = {row["sourceKey"]: {
                    **{key: row[key] for key in ("account", "share", "resourceGroup")},
                    "paths": [item["path"] for item in source_files(row)],
                } for row in rows}
                if previous_paths is not None:
                    op["purgedFiles"] = [
                        {"sourceKey": key, **{field: value[field] for field in ("account", "share", "resourceGroup")}, "path": path}
                        for key, value in sorted(previous_paths.items()) if key in op["sourcePaths"]
                        for path in sorted(set(value["paths"]) - set(op["sourcePaths"][key]["paths"]))
                    ]
                    op["purgedFileCount"] = len(op["purgedFiles"])
            except (AzureApiError, SourcePrerequisiteError) as error:
                logging.warning("Purge source listing unavailable.")
                op["inventoryError"] = str(error)
        path = f"/knowledgesources/{quote(gateway.settings.knowledge_source, safe='')}"
        source = gateway.search("GET", path)
        current = source["searchIndexParameters"]["searchIndexName"]
        if current != target["index"]:
            if current != op["activeIndex"]:
                raise StateConflict("The knowledge source changed during the rebuild.")
            body = editable(source)
            body["searchIndexParameters"]["searchIndexName"] = target["index"]
            gateway.search("PUT", path, body, {"If-Match": source["@odata.etag"]}, (200, 201, 204))
            if gateway.search("GET", path)["searchIndexParameters"]["searchIndexName"] != target["index"]:
                raise AzureApiError("The knowledge-source switch could not be verified.", 409)
        op.update(phase="schedule", switched=True, switchedAt=now())
    elif phase == "schedule":
        previous = op.get("activeLayout") or slot(op["activeIndex"])
        for share in shares:
            _set_schedule(gateway, target["indexers"][share], op["intervals"].get(share))
        for share in shares:
            _set_schedule(gateway, previous["indexers"][share], None)
        if not shares:
            schema = sanitized_schema(gateway.search("GET", f"/indexes/{target['index']}"))
            schema["name"] = op["activeIndex"]
            gateway.search("DELETE", f"/indexes/{op['activeIndex']}", expected=(204, 404))
            gateway.search("PUT", f"/indexes/{op['activeIndex']}", schema, {"If-None-Match": "*"}, (201,))
        op.update(status="completed", phase="schedule")
    else:
        raise StateConflict("The rebuild operation contains an unknown phase.")
    op["updatedAt"] = now()
    return op
