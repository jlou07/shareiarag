import copy
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from azure.core.exceptions import AzureError, HttpResponseError

import index_rebuild
import source_connections
import sync_files
from azure_gateway import AzureApiError, editable, group_from_skillset, parse_share_groups
from settings import identifier
from state import LeaseLost, StateConflict, StateError, now


TERMINAL = {"completed", "accepted", "awaiting_access_verification", "failed", "cancelled"}
SLOTS = ("af-index-all", "af-index-all-b")
BACKGROUND = ("group_change", "rebuild", "source_add")
SCHEDULER = "scheduler"


def audit(state, actor, action, operation_id, detail):
    state["audit"].insert(0, {
        "time": now(), "actor": actor, "action": action,
        "operationId": operation_id, "detail": detail,
    })
    state["audit"] = state["audit"][:500]


def fingerprint(sources, download_group):
    selected = [{
        key: row.get(key) for key in ("name", "index", "share", "sourceKey", "account", "resourceGroup", "groupId", "skillset", "schedule", "etag")
    } for row in sources["indexers"]]
    return hashlib.sha256(json.dumps(
        {"index": sources["activeIndex"], "rows": sorted(selected, key=lambda row: row["name"]), "download": download_group},
        sort_keys=True,
    ).encode()).hexdigest()


def sanitized_schema(index):
    if index.get("permissionFilterOption") != "enabled":
        raise ValueError("The index does not enforce document permissions.")
    if not any(field.get("name") == "group_ids" and field.get("permissionFilter") == "groupIds" for field in index.get("fields", [])):
        raise ValueError("The index does not contain the expected group permission field.")
    body = editable(index)
    for vectorizer in body.get("vectorSearch", {}).get("vectorizers", []):
        parameters = vectorizer.get("azureOpenAIParameters") or {}
        parameters.pop("apiKey", None)
        parameters.pop("authIdentity", None)
    return body


class Controller:
    def __init__(self, gateway, store):
        self.gateway = gateway
        self.store = store

    @contextmanager
    def control_lock(self, wait_seconds=30):
        # Background steps hold the lock briefly; wait for them instead of failing the click.
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                lease = self.store.claim("azure-control")
                break
            except AzureError as error:
                status = getattr(error, "status_code", None)
                logging.warning("azure-control lock busy: status=%s code=%s", status, getattr(error, "error_code", None))
                if time.monotonic() >= deadline:
                    if status in (409, 412):
                        raise StateConflict("Another Azure administration step is still running. Retry in a minute.") from error
                    raise StateError("The Azure administration lock could not be acquired.") from error
                time.sleep(1)
        try:
            self.gateway.bind_lease(lease)
            yield
        finally:
            self.gateway.bind_lease(None)
            try:
                lease.release()
            except AzureError as error:
                raise StateError("The Azure administration lock could not be released; inspect operation status before retrying.") from error

    def _no_maintenance(self, state):
        if any(op["type"] == "source_add" and op["status"] not in (TERMINAL - {"failed"}) for op in state["operations"]):
            raise StateConflict("A source connection is running or failed. Complete or resume it before changing indexing.")
        if any(op["type"] == "group_change" and op["status"] not in TERMINAL for op in state["operations"]):
            raise StateConflict("Permission maintenance is already running. Wait before changing indexing.")
        if any(op["type"] == "group_change" and op["status"] == "failed" for op in state["operations"]):
            raise StateConflict("Permission maintenance failed. Resume it before starting another operation.")
        if any(op["type"] == "rebuild" and op["status"] not in TERMINAL for op in state["operations"]):
            raise StateConflict("An index rebuild is running. Wait for it to finish before changing indexing.")
        if any(op["type"] == "rebuild" and op["status"] == "failed" and op.get("switched") for op in state["operations"]):
            raise StateConflict("An index rebuild failed after switching the agent. Resume it before starting another operation.")

    def rebuild_view(self, state, moment=None):
        moment = moment or datetime.now(timezone.utc)
        settings = index_rebuild.settings_from(state)
        last = next((op for op in state["operations"] if op.get("type") == "rebuild"), None)
        return {
            **{key: settings[key] for key in ("enabled", "intervalHours", "startHour", "updatedAt", "updatedBy")},
            "timezone": "Europe/Paris",
            "nextRun": index_rebuild.next_slot(settings, moment).isoformat() if settings["enabled"] else None,
            "lastOperationId": last["id"] if last else None,
        }

    def update_rebuild_schedule(self, actor, enabled, interval_hours, start_hour):
        index_rebuild.validate_schedule(enabled, interval_hours, start_hour)

        def change(state):
            settings = index_rebuild.settings_from(state)
            settings.update(enabled=enabled, intervalHours=interval_hours, startHour=start_hour,
                            updatedAt=now(), updatedBy=actor)
            # Only future slots trigger: enabling never starts an immediate catch-up rebuild.
            due = index_rebuild.due_slot(settings, datetime.now(timezone.utc))
            settings["lastSlot"] = due.isoformat() if due else None
            state["rebuild"] = settings
            audit(state, actor, "rebuild_schedule", "",
                  f"{'enabled' if enabled else 'disabled'} every {interval_hours}h from {start_hour:02d}:00 Europe/Paris")
            return self.rebuild_view(state)
        return self.store.update(change)

    def clear_logs(self, actor):
        def change(state):
            # Logs hides everything created before logsClearedAt. Records still needed by the worker,
            # resume, the PURGED counter and the Purges tab stay stored; everything else is deleted.
            previous = state.get("logsClearedAt") or ""
            removed = sum(1 for op in state["operations"] if (op.get("createdAt") or "") > previous)
            state["operations"] = [op for op in state["operations"] if op["type"] == "rebuild"
                                   or op["status"] not in TERMINAL
                                   or (op["status"] == "failed" and op["type"] in BACKGROUND)]
            state["logsClearedAt"] = now()
            state["audit"] = []
            logging.info("Logs cleared by %s (%s operations).", actor, removed)
            return {"removed": removed}
        return self.store.update(change)

    def start_rebuild(self, actor, operation_id, mode="targeted"):
        operation_id = identifier(operation_id)

        def reserve(state):
            existing = next((op for op in state["operations"] if op["id"] == operation_id), None)
            if existing:
                if existing["type"] != "rebuild" or existing["actor"] != actor:
                    raise StateConflict("This operation identifier was already used for a different request.")
                return existing
            self._no_maintenance(state)
            operation = index_rebuild.new_operation(operation_id, actor, "manual", mode)
            state["operations"].insert(0, operation)
            audit(state, actor, "start_rebuild", operation_id, "manual")
            return operation
        return self.store.update(reserve)

    def schedule_due_rebuild(self, moment):
        settings = index_rebuild.settings_from(self.store.read())
        if not settings["enabled"]:
            return None
        due = index_rebuild.due_slot(settings, moment)
        if due is None or (settings["lastSlot"] and datetime.fromisoformat(settings["lastSlot"]) >= due):
            return None

        def reserve(state):
            current = index_rebuild.settings_from(state)
            if not current["enabled"] or (current["lastSlot"] and datetime.fromisoformat(current["lastSlot"]) >= due):
                return None
            self._no_maintenance(state)
            operation = index_rebuild.new_operation(str(uuid4()), SCHEDULER, "schedule")
            operation["slot"] = due.isoformat()
            current["lastSlot"] = due.isoformat()
            state["rebuild"] = current
            state["operations"].insert(0, operation)
            audit(state, SCHEDULER, "start_rebuild", operation["id"], "schedule")
            return operation
        return self.store.update(reserve)

    def indexer_action(self, actor, operation_id, name, action, interval):
        with self.control_lock():
            return self._indexer_action(actor, operation_id, name, action, interval)

    def _indexer_action(self, actor, operation_id, name, action, interval):
        operation_id = identifier(operation_id)
        if action not in ("run", "pause", "schedule"):
            raise ValueError("Unsupported indexer action.")
        self._no_maintenance(self.store.read())
        existing = next((op for op in self.store.read()["operations"] if op["id"] == operation_id), None)
        if existing:
            if existing.get("target") != name or existing.get("action") != action or existing.get("interval") != interval:
                raise StateConflict("This operation identifier was already used for a different request.")
            return existing
        operation = {
            "id": operation_id, "type": "indexer", "target": name, "action": action, "interval": interval,
            "actor": actor, "createdAt": now(), "status": "executing",
        }

        def reserve(state):
            self._no_maintenance(state)
            if any(op["id"] == operation_id for op in state["operations"]):
                raise StateConflict("This action is already reserved. Refresh its operation status.")
            state["operations"].insert(0, operation)
            audit(state, actor, action, operation_id, name)
        self.store.update(reserve)
        try:
            result = self.gateway.indexer_action(name, action, interval)
        except (AzureApiError, ValueError) as error:
            self._finish(operation_id, {"status": "failed", "error": str(error)})
            raise
        self._finish(operation_id, result)
        return next(op for op in self.store.read()["operations"] if op["id"] == operation_id)

    def _finish(self, operation_id, result):
        def change(state):
            operation = next(op for op in state["operations"] if op["id"] == operation_id)
            operation.update(result, updatedAt=now())
            audit(state, operation["actor"], operation["status"], operation_id, operation.get("share", operation.get("target", "")))
        self.store.update(change)

    def preview_group(self, actor, share, new_group):
        new_group = identifier(new_group)
        sources = self.gateway.sources()
        rows = [row for row in sources["indexers"] if row["active"] and row.get("sourceKey", row["share"]) == share]
        if len(rows) != 1:
            raise ValueError("Choose one configured active source share.")
        old_group = rows[0]["groupId"]
        if old_group == new_group:
            raise ValueError("This source already uses that group.")
        self.gateway.verify_group(new_group)
        mapping = parse_share_groups(self.gateway.download_settings().get("SHARE_GROUPS", ""))
        if mapping.get(share) != old_group:
            raise StateConflict("Search and download permissions already differ. Resolve that mismatch before changing groups.")
        draft = {
            "id": str(uuid4()), "actor": actor, "share": share, "oldGroup": old_group, "newGroup": new_group,
            "activeIndex": sources["activeIndex"], "fingerprint": fingerprint(sources, mapping.get(share)),
            "createdAt": now(), "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            "impact": {
                "maintenanceRequired": True, "indexesRecreated": list(SLOTS),
                "allSourcesReindexed": True, "sourceFilesDeleted": False,
                "previousSasMaximumMinutes": 5, "accessValidationRequired": True,
            },
        }

        def save(state):
            self._no_maintenance(state)
            state["drafts"].insert(0, draft)
            state["drafts"] = state["drafts"][:50]
            audit(state, actor, "preview_group", draft["id"], share)
        self.store.update(save)
        return draft

    def add_source(self, actor, operation_id, resource_group, account, share, group_id, interval):
        operation_id = identifier(operation_id)
        group_id = identifier(group_id)
        source_connections.account_path(self.gateway.settings, resource_group, account)
        source_connections.validate_share(share)
        if interval is not None and interval not in source_connections.INTERVALS:
            raise ValueError("Choose a supported indexing interval.")
        with self.control_lock():
            state = self.store.read()
            existing = next((op for op in state["operations"] if op["id"] == operation_id), None)
            if existing:
                if any(existing.get(key) != value for key, value in {
                    "type": "source_add", "actor": actor, "account": account, "resourceGroup": resource_group,
                    "share": share, "groupId": group_id, "interval": interval,
                }.items()):
                    raise StateConflict("This operation identifier belongs to another request.")
                return existing
            self._no_maintenance(state)
            self.gateway.verify_storage_share(resource_group, account, share)
            source_connections.connection(account)
            if not self.gateway.settings.key_vault_name:
                raise source_connections.SourcePrerequisiteError(
                    "key_vault_not_configured", "Configure KEY_VAULT_NAME before connecting a source.", "KEY_VAULT_NAME",
                )
            self.gateway.verify_group(group_id)
            download = self.gateway.download_settings()
            if download.get("MULTI_ACCOUNT_SOURCES_VERSION") != "1":
                raise source_connections.SourcePrerequisiteError(
                    "downloader_not_ready",
                    "Deploy the multi-account download Function and set MULTI_ACCOUNT_SOURCES_VERSION=1 before connecting a source.",
                )
            if parse_share_groups(download.get("SHARE_GROUPS", "")).get(source_connections.source_identity(account, share)) not in (None, group_id):
                raise StateConflict("This download source already has a different group.")
            sources = self.gateway.sources()
            if any(row["share"] == share and row.get("account", self.gateway.settings.files_account) == account
                   for row in sources["indexers"]):
                raise StateConflict("This account/share is already connected.")
            template = next((row for row in sources["indexers"] if row["active"]), None)
            saved = state.get("sourceTemplate")
            if template is None and not saved:
                raise StateConflict("No share configuration is available to copy. Restore a source with the deployment scripts.")
            source_connections.skillset(
                self.gateway.search("GET", f"/skillsets/{template['skillset']}") if template else saved["skillset"],
                "validation", sources["activeIndex"], group_id, share)
            operation = {
                "id": operation_id, "type": "source_add", "actor": actor, "status": "queued", "phase": "prepare",
                "resourceGroup": resource_group, "subscriptionId": self.gateway.settings.subscription,
                "account": account, "share": share, "sourceKey": source_connections.source_identity(account, share),
                "groupId": group_id, "interval": interval, "activeIndex": sources["activeIndex"],
                **({"templateSkillset": template["skillset"], "templateIndexer": template["name"]} if template
                   else {"template": copy.deepcopy(saved)}),
                "createdAt": now(), "updatedAt": now(),
                "deadline": (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat(),
            }

            def reserve(candidate):
                self._no_maintenance(candidate)
                candidate["operations"].insert(0, operation)
                audit(candidate, actor, "add_source", operation_id, operation["sourceKey"])
                return operation
            return self.store.update(reserve)

    def remove_source(self, actor, operation_id, source_key):
        operation_id = identifier(operation_id)
        if not isinstance(source_key, str) or not source_key:
            raise ValueError("Choose a connected source.")
        with self.control_lock():
            state = self.store.read()
            existing = next((op for op in state["operations"] if op["id"] == operation_id), None)
            if existing:
                if existing.get("type") != "source_remove" or existing.get("sourceKey") != source_key:
                    raise StateConflict("This operation identifier was already used for a different request.")
                return existing
            self._no_maintenance(self._without_failed_add(state, source_key))
            operation = {"id": operation_id, "type": "source_remove", "sourceKey": source_key, "actor": actor,
                         "createdAt": now(), "status": "executing"}

            def reserve(candidate):
                self._no_maintenance(self._without_failed_add(candidate, source_key))
                candidate["operations"].insert(0, operation)
                audit(candidate, actor, "remove_source", operation_id, source_key)
            self.store.update(reserve)
            try:
                result = self.gateway.remove_source(source_key)
            except (AzureApiError, ValueError) as error:
                self._finish(operation_id, {"status": "failed", "error": str(error)})
                raise

            def complete(candidate):
                for op in candidate["operations"]:
                    if op["type"] == "source_add" and op.get("sourceKey") == source_key and                     op["status"] not in (TERMINAL - {"failed"}):
                        op.update(status="cancelled", updatedAt=now())
                # A purge removes this source's chunks (targeted) or recreates the index from the remaining sources.
                rebuild = next((op for op in candidate["operations"] if op["type"] == "rebuild"
                                and op["status"] not in TERMINAL and not op.get("switched")), None)
                if rebuild:
                    rebuild.update(status="queued", phase=index_rebuild.initial_phase(rebuild), cursor=0, updatedAt=now(),
                                   deadline=(datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat())
                    audit(candidate, actor, "restart_rebuild", rebuild["id"], f"after removing {source_key}")
                else:
                    rebuild = index_rebuild.new_operation(str(uuid4()), actor, "manual")
                    candidate["operations"].insert(0, rebuild)
                    audit(candidate, actor, "start_rebuild", rebuild["id"], f"after removing {source_key}")
                operation = next(op for op in candidate["operations"] if op["id"] == operation_id)
                template = result.pop("template", None)
                if template:
                    candidate["sourceTemplate"] = template
                operation.update(result, rebuildId=rebuild["id"], updatedAt=now())
                audit(candidate, actor, "completed", operation_id, source_key)
                return operation
            return self.store.update(complete)

    @staticmethod
    def _without_failed_add(state, source_key):
        # A purge that has not switched the agent yet is restarted by the removal instead of blocking it.
        return {**state, "operations": [
            op for op in state["operations"]
            if not (op["type"] == "source_add" and op.get("sourceKey") == source_key)
            and not (op["type"] == "rebuild" and op["status"] not in TERMINAL and not op.get("switched"))
        ]}

    def apply_group(self, actor, draft_id, confirmation):
        with self.control_lock():
            return self._apply_group(actor, draft_id, confirmation)

    def _apply_group(self, actor, draft_id, confirmation):
        if confirmation not in ("REBUILD", "RECONSTRUIRE"):
            raise ValueError("Type REBUILD to confirm the maintenance and index recreation.")
        existing = next((op for op in self.store.read()["operations"] if op["id"] == draft_id), None)
        if existing:
            if existing["actor"] != actor or existing["type"] != "group_change":
                raise StateConflict("This operation belongs to another request.")
            return {key: value for key, value in existing.items() if key != "schema"}
        draft = next((d for d in self.store.read()["drafts"] if d["id"] == draft_id and d["actor"] == actor), None)
        if draft is None or datetime.fromisoformat(draft["expiresAt"]) <= datetime.now(timezone.utc):
            raise StateConflict("This preview expired or belongs to another administrator.")
        sources = self.gateway.sources()
        mapping = parse_share_groups(self.gateway.download_settings().get("SHARE_GROUPS", ""))
        if fingerprint(sources, mapping.get(draft["share"])) != draft["fingerprint"]:
            raise StateConflict("The live configuration changed. Generate a new preview.")
        if any(not row["active"] and row["schedule"] for row in sources["indexers"]):
            raise StateConflict("Inactive-slot scheduling must be reviewed before permission maintenance.")
        operation = {
            "id": draft["id"], "type": "group_change", "status": "queued", "phase": "snapshot",
            "actor": actor, "share": draft["share"], "oldGroup": draft["oldGroup"], "newGroup": draft["newGroup"],
            "activeIndex": sources["activeIndex"], "fingerprint": draft["fingerprint"],
            "indexers": copy.deepcopy(sources["indexers"]), "createdAt": now(), "updatedAt": now(),
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat(), "cursor": 0,
        }

        def reserve(state):
            existing = next((op for op in state["operations"] if op["id"] == operation["id"]), None)
            if existing:
                return existing
            self._no_maintenance(state)
            state["operations"].insert(0, operation)
            audit(state, actor, "apply_group", operation["id"], operation["share"])
            return operation
        result = self.store.update(reserve)
        return {key: value for key, value in result.items() if key != "schema"}

    def retry(self, actor, operation_id):
        def change(state):
            operation = next((op for op in state["operations"] if op["id"] == operation_id), None)
            if not operation or operation["type"] not in BACKGROUND or operation["status"] != "failed":
                raise StateConflict("Only failed maintenance or rebuild operations can be resumed.")
            if any(op["id"] != operation_id and op["type"] in BACKGROUND and op["status"] not in TERMINAL
                   for op in state["operations"]):
                raise StateConflict("Another background operation is running. Wait before resuming.")
            if any(op["id"] != operation_id and op["type"] == "source_add" and op["status"] == "failed"
                   for op in state["operations"]):
                raise StateConflict("A source connection failed. Resume it before another background operation.")
            if operation["type"] == "rebuild":
                if not operation.get("switched"):
                    if any(op["type"] == "group_change" and op["status"] == "failed" for op in state["operations"]):
                        raise StateConflict("Permission maintenance failed. Resume it before rebuilding.")
                    # Before the switch the active index was untouched (a targeted purge only deletes chunks of
                    # missing files, so repeating it is safe): restart from the first phase.
                    operation.update(phase=index_rebuild.initial_phase(operation), cursor=0)
                operation.update(status="queued", updatedAt=now(), error=None,
                                 deadline=(datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat())
                audit(state, actor, "resume_rebuild", operation_id, "index rebuild")
                return operation
            if operation["type"] == "source_add":
                operation.update(status="queued", updatedAt=now(), error=None,
                                 deadline=(datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat())
                if operation["phase"] == "wait":
                    operation["phase"] = "run"
                audit(state, actor, "resume_source", operation_id, operation["sourceKey"])
                return operation
            operation.update(status="queued", updatedAt=now(), error=None,
                             deadline=(datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat())
            if operation["phase"] == "wait":
                operation.update(phase="reset", cursor=0)
            audit(state, actor, "resume_group", operation_id, operation["share"])
            return operation
        return self.store.update(change)

    def cancel_unstarted(self, actor, operation_id):
        def change(state):
            operation = next((op for op in state["operations"] if op["id"] == operation_id), None)
            if (
                not operation or operation["type"] != "group_change"
                or operation["phase"] != "snapshot" or operation["status"] not in ("queued", "failed")
            ):
                raise StateConflict("Only maintenance that has not changed Azure resources can be cancelled.")
            operation.update(status="cancelled", updatedAt=now())
            audit(state, actor, "cancel_unstarted_group", operation_id, operation["share"])
            return operation
        return self.store.update(change)

    def step(self, operation):
        if operation["type"] == "source_add":
            return source_connections.step(self.gateway, operation)
        if operation["type"] == "rebuild":
            previous = None
            if operation.get("phase") == "switch" and "sourcePaths" not in operation:
                previous = index_rebuild.previous_source_paths(self.store.read()["operations"], operation["id"])
            return index_rebuild.step(self.gateway, sanitized_schema, operation, previous)
        op = copy.deepcopy(operation)
        if datetime.now(timezone.utc) >= datetime.fromisoformat(op["deadline"]):
            raise StateConflict("Maintenance timed out. Inspect Azure status and resume explicitly; no success was assumed.")
        active = [row for row in op["indexers"] if row["active"]]
        phase = op["phase"]
        cursor = op.get("cursor", 0)
        if phase == "snapshot":
            sources = self.gateway.sources()
            mapping = parse_share_groups(self.gateway.download_settings().get("SHARE_GROUPS", ""))
            if fingerprint(sources, mapping.get(op["share"])) != op["fingerprint"]:
                raise StateConflict("Configuration changed after confirmation; maintenance has not started.")
            op["schema"] = sanitized_schema(self.gateway.search("GET", f"/indexes/{op['activeIndex']}"))
            op.update(phase="pause", status="running", cursor=0)
        elif phase in ("pause", "restore"):
            row = active[cursor]
            path = f"/indexers/{row['name']}"
            current = self.gateway.search("GET", path)
            body = editable(current)
            interval = row["schedule"] if phase == "restore" else None
            if interval:
                body["schedule"] = {"interval": interval}
            else:
                body.pop("schedule", None)
            if body != editable(current):
                self.gateway.search("PUT", path, body, {"If-Match": current["@odata.etag"]}, (200, 201, 204))
            op["cursor"] += 1
            if op["cursor"] == len(active):
                op.update(phase="idle" if phase == "pause" else "finish", cursor=0)
        elif phase == "idle":
            for row in op["indexers"]:
                result = self.gateway.search("GET", f"/indexers/{row['name']}/status").get("lastResult") or {}
                if result.get("status") == "inProgress":
                    return op
            op["phase"] = "block_download"
        elif phase == "block_download":
            self.gateway.set_download_group(op["share"], None, {op["oldGroup"], None})
            op.update(phase="delete", cursor=0)
        elif phase == "delete":
            self.gateway.search("DELETE", f"/indexes/{SLOTS[cursor]}", expected=(204, 404))
            op["cursor"] += 1
            if op["cursor"] == len(SLOTS):
                op.update(phase="skills", cursor=0)
        elif phase == "skills":
            rows = [row for row in op["indexers"] if row.get("sourceKey", row["share"]) == op["share"]]
            path = f"/skillsets/{rows[cursor]['skillset']}"
            current = self.gateway.search("GET", path)
            group = group_from_skillset(current)
            if group not in (op["oldGroup"], op["newGroup"]):
                raise StateConflict("A source group was changed outside this maintenance operation.")
            if group != op["newGroup"]:
                body = editable(current)
                for skill in body["skills"]:
                    if skill.get("name") == "group-id":
                        for item in skill["inputs"]:
                            if item.get("name") == "whenTrue":
                                item["source"] = f"= '{op['newGroup']}'"
                self.gateway.search("PUT", path, body, {"If-Match": current["@odata.etag"]}, (200, 201, 204))
            op["cursor"] += 1
            if op["cursor"] == len(rows):
                op.update(phase="create", cursor=0)
        elif phase == "create":
            index = SLOTS[cursor]
            current = self.gateway.search("GET", f"/indexes/{index}", expected=(200, 404))
            if current is None:
                schema = copy.deepcopy(op["schema"])
                schema["name"] = index
                self.gateway.search("PUT", f"/indexes/{index}", schema, {"If-None-Match": "*"}, (201,))
            else:
                sanitized_schema(current)
            op["cursor"] += 1
            if op["cursor"] == len(SLOTS):
                op.update(phase="reset", cursor=0, previousStarts={})
        elif phase == "reset":
            row = active[cursor]
            self.gateway.search("POST", f"/indexers/{row['name']}/reset", expected=(204,))
            result = self.gateway.search("GET", f"/indexers/{row['name']}/status").get("lastResult") or {}
            op.setdefault("previousStarts", {})[row["name"]] = result.get("startTime")
            op["cursor"] += 1
            if op["cursor"] == len(active):
                op.update(phase="run", cursor=0)
        elif phase == "run":
            row = active[cursor]
            result = self.gateway.search("GET", f"/indexers/{row['name']}/status").get("lastResult") or {}
            already_started = result.get("startTime") and result["startTime"] != op["previousStarts"].get(row["name"])
            if not already_started:
                self.gateway.search("POST", f"/indexers/{row['name']}/run", expected=(202,))
            op["cursor"] += 1
            if op["cursor"] == len(active):
                op.update(phase="wait", cursor=0)
        elif phase == "wait":
            results = []
            for row in active:
                result = self.gateway.search("GET", f"/indexers/{row['name']}/status").get("lastResult") or {}
                if not result.get("startTime") or result["startTime"] == op["previousStarts"].get(row["name"]) or result.get("status") == "inProgress":
                    return op
                if result.get("status") != "success" or result.get("itemsFailed"):
                    raise StateConflict(f"Indexer {row['name']} did not complete successfully. Downloads remain restricted; inspect and resume.")
                results.append({
                    "name": row["name"], "itemsProcessed": result.get("itemsProcessed", 0),
                    "itemsFailed": result.get("itemsFailed", 0), "warningCount": len(result.get("warnings") or []),
                })
            op.update(phase="verify", results=results, verificationStartedAt=now())
        elif phase == "verify":
            schema = self.gateway.search("GET", f"/indexes/{op['activeIndex']}")
            sanitized_schema(schema)
            count = self.gateway.search("GET", f"/indexes/{op['activeIndex']}/stats").get("documentCount")
            if not isinstance(count, int) or count < 0:
                raise AzureApiError("Azure did not return valid index statistics.")
            if count == 0 and sum(row["itemsProcessed"] for row in op["results"]) > 0:
                elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(op["verificationStartedAt"])
                if elapsed.total_seconds() >= 420:
                    raise StateConflict("Index statistics remain empty despite processed source items. Access remains restricted.")
                return op
            if op.get("observedCount") != count:
                op["observedCount"] = count
                return op
            op.update(phase="publish_download", documentCount=count)
        elif phase == "publish_download":
            self.gateway.set_download_group(op["share"], op["newGroup"], {None, op["newGroup"]})
            op.update(phase="restore", cursor=0)
        elif phase == "finish":
            sources = self.gateway.sources()
            if sources["activeIndex"] != op["activeIndex"]:
                raise StateConflict("The knowledge-source target changed during maintenance.")
            groups = {row["groupId"] for row in sources["indexers"] if row.get("sourceKey", row["share"]) == op["share"]}
            if groups != {op["newGroup"]}:
                raise StateConflict("The expected group is not consistent across source slots.")
            op.update(
                status="awaiting_access_verification",
                message="Configuration published and indexing succeeded. Validate authorized and unauthorized users in Search, Teams/Copilot and file opening. Previously issued SAS links may remain valid for five minutes.",
            )
            op.pop("schema", None)
        else:
            raise StateError("The maintenance operation contains an unknown phase.")
        op["updatedAt"] = now()
        return op


class Worker:
    def __init__(self, controller):
        self.controller = controller
        self._lock = threading.Lock()
        self._thread = None
        self.last_error = None
        self._next_sync_listing = 0

    def start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="console-maintenance", daemon=True)
                self._thread.start()

    @property
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        while True:
            try:
                self.tick()
                self.last_error = None
            except StateConflict:
                logging.debug("Maintenance will retry after an overlapping administration change.")
            except StateError as error:
                self.last_error = str(error)
                logging.error("Console maintenance could not persist or safely own its next step.")
            threading.Event().wait(10)

    def tick(self):
        state = self.controller.store.read()
        operation = next((op for op in reversed(state["operations"]) if op["type"] in BACKGROUND and op["status"] not in TERMINAL), None)
        if not operation:
            self.controller.schedule_due_rebuild(datetime.now(timezone.utc))
            if time.monotonic() >= self._next_sync_listing:
                self._next_sync_listing = time.monotonic() + 60
                try:
                    sync_files.record(self.controller.gateway, self.controller.store)
                except (AzureApiError, StateError):
                    logging.warning("Sync file counters could not be refreshed.")
            return
        with self.controller.control_lock():
            current = next(op for op in self.controller.store.read()["operations"] if op["id"] == operation["id"])
            if current["status"] in TERMINAL:
                return
            try:
                result = self.controller.step(current)
            except LeaseLost:
                raise
            except (AzureApiError, StateError, ValueError, KeyError, TypeError) as error:
                result = {**current, "status": "failed", "error": str(error), "updatedAt": now()}
                logging.error("Background %s operation failed at phase %s.", current["type"], current["phase"])
            def save(candidate):
                index = next(i for i, op in enumerate(candidate["operations"]) if op["id"] == result["id"])
                if candidate["operations"][index] != current:
                    raise StateConflict("The operation changed while this worker was checking it.")
                candidate["operations"][index] = result
                if result.get("status") == "completed" and result.get("sourcePaths"):
                    # Only the latest purge listing is needed as the next purge baseline.
                    for other in candidate["operations"]:
                        if other is not result and other["type"] == "rebuild":
                            other.pop("sourcePaths", None)
                if result["status"] in TERMINAL:
                    audit(candidate, result["actor"], result["status"], result["id"], result.get("share", "index rebuild"))
            self.controller.gateway.renew_lease()
            self.controller.store.update(save)
