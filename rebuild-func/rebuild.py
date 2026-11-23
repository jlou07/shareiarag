"""Blue/green rebuild of the permission-filtered Search index.

Each run rebuilds the inactive slot into an empty index, validates it, points
the knowledge source at it, and moves the five-minute incremental schedule to
the new slot. The previous index is left untouched as a rollback target until
the next run rebuilds it.
"""

import copy
import logging
import time
from dataclasses import dataclass, field

API_VERSION = "2026-08-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
KNOWLEDGE_SOURCE = "af-ks-all"
SHARES = ("admin", "alluser")
INCREMENTAL_INTERVAL = "PT5M"
SLOTS = {
    "a": {
        "index": "af-index-all",
        "skillsets": {s: f"af-skills-all-{s}" for s in SHARES},
        "indexers": {s: f"af-indexer-all-{s}" for s in SHARES},
    },
    "b": {
        "index": "af-index-all-b",
        "skillsets": {s: f"af-skills-all-b-{s}" for s in SHARES},
        "indexers": {s: f"af-indexer-all-b-{s}" for s in SHARES},
    },
}
DATA_SOURCES = {s: f"af-ds-{s}" for s in SHARES}


class RebuildError(RuntimeError):
    pass


@dataclass
class Report:
    previous_index: str = ""
    new_index: str = ""
    items_processed: dict = field(default_factory=dict)
    previous_chunks: int = 0
    new_chunks: int = 0
    switched: bool = False


class SearchApi:
    """Minimal Search REST client using either an admin key or an Entra credential."""

    def __init__(self, session, endpoint, api_key=None, credential=None):
        if bool(api_key) == bool(credential):
            raise ValueError("Provide exactly one of api_key or credential.")
        self._session = session
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._credential = credential

    def _headers(self, extra=None):
        if self._api_key:
            headers = {"api-key": self._api_key}
        else:
            headers = {"Authorization": "Bearer " + self._credential.get_token(SEARCH_SCOPE).token}
        headers.update(extra or {})
        return headers

    def call(self, method, path, body=None, headers=None, expected=(200,)):
        response = self._session.request(
            method,
            self._endpoint + path,
            params={"api-version": API_VERSION},
            headers=self._headers(headers),
            json=body,
            timeout=120,
        )
        if response.status_code not in expected:
            try:
                message = response.json().get("error", {}).get("message", "")
            except ValueError:
                message = ""
            raise RebuildError(f"{method} {path} returned HTTP {response.status_code}: {message[:300]}")
        if response.status_code in (204, 404) or not response.content:
            return None
        return response.json()

    def get(self, path, missing_ok=False):
        return self.call("GET", path, expected=(200, 404) if missing_ok else (200,))


def editable(definition):
    return {k: copy.deepcopy(v) for k, v in definition.items() if not k.startswith("@odata.")}


def other_slot(slot):
    return "b" if slot == "a" else "a"


def active_slot(index_name):
    for slot, names in SLOTS.items():
        if names["index"] == index_name:
            return slot
    raise RebuildError(f"The knowledge source targets an unexpected index: {index_name}")


def index_definition(active, name):
    """Copy the active index schema; query vectorization uses the Search managed identity."""
    if active.get("permissionFilterOption") != "enabled":
        raise RebuildError("The active index does not enforce permission filtering.")
    fields = {f["name"]: f for f in active.get("fields", [])}
    if fields.get("group_ids", {}).get("permissionFilter") != "groupIds":
        raise RebuildError("The active index has no group permission field.")
    definition = editable(active)
    definition["name"] = name
    for vectorizer in definition.get("vectorSearch", {}).get("vectorizers", []):
        params = vectorizer.get("azureOpenAIParameters") or {}
        params.pop("apiKey", None)
        params.pop("authIdentity", None)
    return definition


def skillset_definition(source, name, target_index):
    definition = editable(source)
    definition["name"] = name
    for skill in definition.get("skills", []):
        if skill.get("@odata.type") == "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill":
            skill.pop("apiKey", None)
            skill.pop("authIdentity", None)
    selectors = definition.get("indexProjections", {}).get("selectors", [])
    if not selectors:
        raise RebuildError(f"Skillset {source.get('name')} has no index projection.")
    for selector in selectors:
        mappings = {m["name"]: m.get("source") for m in selector.get("mappings", [])}
        if mappings.get("group_ids") != "/document/group_ids":
            raise RebuildError(f"Skillset {source.get('name')} does not project group_ids.")
        selector["targetIndexName"] = target_index
    return definition


class Rebuilder:
    def __init__(self, api, sleep=time.sleep, clock=time.monotonic,
                 indexer_timeout=1200, stats_timeout=420, poll_seconds=10, min_ratio=0.5):
        self.api = api
        self.sleep = sleep
        self.clock = clock
        self.indexer_timeout = indexer_timeout
        self.stats_timeout = stats_timeout
        self.poll_seconds = poll_seconds
        self.min_ratio = min_ratio

    def ensure_slot(self, slot, template_slot):
        """Create the slot's skillsets and indexers from the active slot when missing."""
        names, template = SLOTS[slot], SLOTS[template_slot]
        for share in SHARES:
            skillset_name = names["skillsets"][share]
            existing = self.api.get(f"/skillsets/{skillset_name}", missing_ok=True)
            if existing is None:
                source = self.api.get(f"/skillsets/{template['skillsets'][share]}")
                self.api.call(
                    "PUT", f"/skillsets/{skillset_name}",
                    skillset_definition(source, skillset_name, names["index"]),
                    headers={"If-None-Match": "*"}, expected=(201,),
                )
                logging.info("Created skillset %s.", skillset_name)
            else:
                targets = {s.get("targetIndexName") for s in
                           existing.get("indexProjections", {}).get("selectors", [])}
                if targets != {names["index"]}:
                    raise RebuildError(f"Skillset {skillset_name} projects into {sorted(targets)}.")

            indexer_name = names["indexers"][share]
            existing = self.api.get(f"/indexers/{indexer_name}", missing_ok=True)
            if existing is None:
                source = editable(self.api.get(f"/indexers/{template['indexers'][share]}"))
                source.update(name=indexer_name, targetIndexName=names["index"], skillsetName=skillset_name)
                source.pop("schedule", None)
                self.api.call("PUT", f"/indexers/{indexer_name}", source,
                              headers={"If-None-Match": "*"}, expected=(201,))
                logging.info("Created indexer %s.", indexer_name)
                existing = self.api.get(f"/indexers/{indexer_name}")
            if (existing.get("targetIndexName") != names["index"]
                    or existing.get("skillsetName") != skillset_name
                    or existing.get("dataSourceName") != DATA_SOURCES[share]):
                raise RebuildError(f"Indexer {indexer_name} has an unexpected configuration.")

    def set_schedule(self, indexer_name, interval):
        indexer = self.api.get(f"/indexers/{indexer_name}", missing_ok=True)
        if indexer is None:
            return
        if (indexer.get("schedule") or {}).get("interval") == interval:
            return
        body = editable(indexer)
        if interval:
            body["schedule"] = {"interval": interval}
        else:
            body.pop("schedule", None)
        self.api.call("PUT", f"/indexers/{indexer_name}", body,
                      headers={"If-Match": indexer["@odata.etag"]}, expected=(200, 201, 204))

    def last_result(self, indexer_name):
        return (self.api.get(f"/indexers/{indexer_name}/status") or {}).get("lastResult") or {}

    def wait_idle(self, indexer_name, deadline):
        while self.last_result(indexer_name).get("status") == "inProgress":
            if self.clock() >= deadline:
                raise RebuildError(f"Indexer {indexer_name} is still running.")
            self.sleep(self.poll_seconds)

    def run_indexer(self, indexer_name):
        deadline = self.clock() + self.indexer_timeout
        # A newly created indexer starts automatically; let it finish before the explicit full run.
        self.wait_idle(indexer_name, deadline)
        self.api.call("POST", f"/indexers/{indexer_name}/reset", expected=(204,))
        previous = self.last_result(indexer_name).get("startTime")
        self.api.call("POST", f"/indexers/{indexer_name}/run", expected=(202,))
        while self.clock() < deadline:
            self.sleep(self.poll_seconds)
            result = self.last_result(indexer_name)
            if result.get("startTime") == previous or result.get("status") == "inProgress":
                continue
            if result.get("status") != "success" or result.get("itemsFailed"):
                raise RebuildError(
                    f"Indexer {indexer_name} finished with status={result.get('status')} "
                    f"failed={result.get('itemsFailed')}: {(result.get('errorMessage') or '')[:300]}"
                )
            return result.get("itemsProcessed") or 0
        raise RebuildError(f"Indexer {indexer_name} did not finish in time.")

    def chunk_count(self, index_name):
        return (self.api.get(f"/indexes/{index_name}/stats") or {}).get("documentCount") or 0

    def wait_for_chunks(self, index_name):
        """Index statistics lag behind indexing; wait for two equal non-zero readings."""
        deadline = self.clock() + self.stats_timeout
        last = None
        while self.clock() < deadline:
            count = self.chunk_count(index_name)
            if count and count == last:
                return count
            last = count
            self.sleep(self.poll_seconds)
        raise RebuildError(f"Index {index_name} statistics did not stabilize with documents.")

    def check_permissions(self, index_name):
        """Without a user token, a permission-filtered index must return nothing.

        The vector query also proves that query-time vectorization works.
        """
        result = self.api.call("POST", f"/indexes/{index_name}/docs/search", {
            "search": "*",
            "vectorQueries": [{"kind": "text", "text": "document", "fields": "text_vector", "k": 1}],
            "count": True,
            "top": 1,
        })
        if result.get("@odata.count") or result.get("value"):
            raise RebuildError(f"Index {index_name} returned documents without a user token.")

    def run(self):
        report = Report()
        source = self.api.get(f"/knowledgesources/{KNOWLEDGE_SOURCE}")
        report.previous_index = source["searchIndexParameters"]["searchIndexName"]
        current = active_slot(report.previous_index)
        target = other_slot(current)
        names = SLOTS[target]
        report.new_index = names["index"]

        for share in SHARES:
            name = names["indexers"][share]
            if self.api.get(f"/indexers/{name}", missing_ok=True) is not None:
                if self.last_result(name).get("status") == "inProgress":
                    raise RebuildError("A rebuild indexer is already running; skipping this cycle.")
                self.set_schedule(name, None)

        active_index = self.api.get(f"/indexes/{report.previous_index}")
        definition = index_definition(active_index, names["index"])
        self.api.call("DELETE", f"/indexes/{names['index']}", expected=(204, 404))
        self.api.call("PUT", f"/indexes/{names['index']}", definition,
                      headers={"If-None-Match": "*"}, expected=(201,))
        logging.info("Recreated empty index %s.", names["index"])
        self.ensure_slot(target, current)

        for share in SHARES:
            report.items_processed[share] = self.run_indexer(names["indexers"][share])
        report.new_chunks = self.wait_for_chunks(names["index"])
        report.previous_chunks = self.chunk_count(report.previous_index)
        if report.previous_chunks >= 10 and report.new_chunks < report.previous_chunks * self.min_ratio:
            raise RebuildError(
                f"Refusing to switch: {report.new_chunks} new chunks versus "
                f"{report.previous_chunks} active chunks."
            )
        self.check_permissions(names["index"])

        source = self.api.get(f"/knowledgesources/{KNOWLEDGE_SOURCE}")
        if source["searchIndexParameters"]["searchIndexName"] != report.previous_index:
            raise RebuildError("The knowledge source changed during the rebuild.")
        body = editable(source)
        body["searchIndexParameters"]["searchIndexName"] = names["index"]
        self.api.call("PUT", f"/knowledgesources/{KNOWLEDGE_SOURCE}", body,
                      headers={"If-Match": source["@odata.etag"]}, expected=(200, 201, 204))
        if self.api.get(f"/knowledgesources/{KNOWLEDGE_SOURCE}")["searchIndexParameters"][
                "searchIndexName"] != names["index"]:
            raise RebuildError("The knowledge-source switch did not persist.")
        report.switched = True

        for share in SHARES:
            self.set_schedule(names["indexers"][share], INCREMENTAL_INTERVAL)
        for share in SHARES:
            self.set_schedule(SLOTS[current]["indexers"][share], None)
        return report
