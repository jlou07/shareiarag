import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from azure_gateway import AzureApiError, AzureGateway
from documents import source_files
import sync_files
from index_rebuild import job_counters, job_files
from operations import Controller
from state import MemoryState
import test_console
from test_console import ADMIN, GROUP_A, FakeGateway, SETTINGS


def file(path, share="admin-share"):
    return {"sourceKey": share, "account": "shareiarag", "share": share,
            "resourceGroup": SETTINGS.resource_group, "path": path}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryState()
    def test_overview_reads_resource_sections_concurrently_and_retains_partial_errors(self):
        gateway = AzureGateway(SETTINGS, Mock())
        barrier = threading.Barrier(6)
        def sources():
            barrier.wait(timeout=2)
            return {"activeIndex": "af-index-all", "indexers": []}
        def resource(provider, name, *_args):
            barrier.wait(timeout=2)
            if provider == "Microsoft.Web/sites":
                raise AzureApiError("Downloads unavailable.")
            return {"name": name}
        def hosting(*_args):
            barrier.wait(timeout=2)
            return {"name": "hosting", "sku": {"name": "B1"}, "location": "francecentral", "id": "hosting-id"}
        gateway.sources = sources
        gateway.source_names = lambda value: value
        gateway.resource = resource
        gateway.arm = hosting
        gateway.search = Mock(return_value={"documentCount": 17})
        sections = gateway.overview()["sections"]
        self.assertEqual(sections["downloads"], {"status": "error", "error": "Downloads unavailable."})
        self.assertEqual(sections["index"]["data"]["documentCount"], 17)
        self.assertTrue(all(section["status"] == "ok" for name, section in sections.items() if name != "downloads"))

    def test_gateway_reuses_connection_pools_across_executor_lifetimes(self):
        gateway = AzureGateway(SETTINGS, Mock())
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(gateway._session).result()
        with ThreadPoolExecutor(max_workers=1) as executor:
            second = executor.submit(gateway._session).result()
        self.assertIsNot(first, second)
        self.assertIs(first.get_adapter("https://management.azure.com"),
                      second.get_adapter("https://management.azure.com"))
        self.assertIs(first.get_adapter("https://srch-shareiarag.search.windows.net"),
                      second.get_adapter("https://srch-shareiarag.search.windows.net"))

    def test_gateway_serializes_token_cache_misses_per_audience(self):
        gateway = AzureGateway(SETTINGS, Mock())
        started, release = threading.Event(), threading.Event()
        def acquire(scope):
            if scope == "search":
                started.set()
                if not release.wait(timeout=3):
                    raise AssertionError("Token acquisition was not released.")
            return Mock(token=scope)
        gateway.credential.get_token.side_effect = acquire
        with ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(gateway._token, "search")
            try:
                self.assertTrue(started.wait(timeout=2))
                second = executor.submit(gateway._token, "search")
                self.assertEqual(executor.submit(gateway._token, "arm").result(timeout=2), "arm")
                self.assertEqual(gateway.credential.get_token.call_count, 2)
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2), "search")
            self.assertEqual(second.result(timeout=2), "search")

    @patch("documents.connection", return_value=("private", "private"))
    @patch("documents.ShareClient")
    def test_recursive_storage_listing_counts_files_only(self, sdk, _connection):
        client = sdk.from_connection_string.return_value.__enter__.return_value
        client.get_directory_client.return_value.list_directories_and_files.side_effect = [
            [{"name": "folder", "is_directory": True}, {"name": "root.txt", "is_directory": False}],
            [{"name": "nested.txt", "is_directory": False}],
        ]
        row = self.gateway.sources()["indexers"][2]
        result = source_files(row)
        self.assertEqual([item["path"] for item in result], ["folder/nested.txt", "root.txt"])


    def test_verified_group_names_without_guessing_or_recursive_directory_listing(self):
        gateway = AzureGateway(replace(SETTINGS, directory_enabled=True), Mock())
        gateway.request = Mock(return_value={"id": GROUP_A, "displayName": "Verified Team"})
        sources = {"indexers": [{"groupId": GROUP_A}, {"groupId": GROUP_A}]}
        result = gateway.source_names(sources)
        self.assertEqual(result["indexers"][0]["groupName"], "Verified Team")
        self.assertNotIn("groupName", sources["indexers"][0])
        gateway.source_names(sources)
        gateway.request.assert_called_once()



class PurgeCounterTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryState()
        self.store.claim = Mock(return_value=Mock())
        self.listing = {}
        patcher = patch("documents.source_files",
                        side_effect=lambda row: [file(path, row["share"]) for path in self.listing.get(row["share"], [])])
        self.source_files = patcher.start()
        self.addCleanup(patcher.stop)

    def purge(self):
        controller = Controller(self.gateway, self.store)
        op = controller.start_rebuild(ADMIN, str(uuid4()), "rebuild")
        for _ in range(80):
            op = controller.step(op)
            if op["status"] == "completed":
                break
        self.assertEqual(op["status"], "completed")
        self.store.data["operations"] = [op if item["id"] == op["id"] else item for item in self.store.data["operations"]]
        return op

    def test_first_purge_records_a_baseline_then_counts_files_deleted_since_the_previous_purge(self):
        self.listing = {"admin-share": ["keep.txt", "removed.txt"], "alluser-share": ["other.txt"]}
        first = self.purge()
        self.assertNotIn("purgedFileCount", first)
        self.assertEqual(first["sourcePaths"]["admin-share"]["paths"], ["keep.txt", "removed.txt"])
        self.listing = {"admin-share": ["keep.txt", "new.txt"], "alluser-share": ["other.txt"]}
        second = self.purge()
        self.assertEqual(second["purgedFileCount"], 1)
        self.assertEqual(second["purgedFiles"], [file("removed.txt")])
        counters = {row["sourceKey"]: row["lastPurge"] for row in
                    job_counters(self.gateway.sources(), self.store.data["operations"])["indexers"] if row["active"]}
        self.assertEqual(counters["admin-share"]["purgedCount"], 1)
        self.assertEqual(counters["alluser-share"]["purgedCount"], 0)
        self.assertEqual(counters["admin-share"]["indexedCount"], 2)
        self.assertEqual(counters["admin-share"]["at"], second["switchedAt"])
        self.listing = {"admin-share": ["keep.txt"], "alluser-share": ["other.txt"]}
        self.purge()
        operations, sources = self.store.data["operations"], self.gateway.sources()
        counters = {row["sourceKey"]: row["lastPurge"] for row in job_counters(sources, operations)["indexers"] if row["active"]}
        self.assertEqual(counters["admin-share"]["purgedCount"], 2, "purged counts every purge, not only the last")
        self.assertEqual(sorted(job_files(sources, operations, {}, "admin-share", "purged")[0]), ["new.txt", "removed.txt"])

    def test_listing_failure_keeps_the_purge_and_reports_unknown_not_zero(self):
        self.source_files.side_effect = AzureApiError("Storage unavailable.")
        op = self.purge()
        self.assertTrue(op["switched"])
        self.assertEqual(op["inventoryError"], "Storage unavailable.")
        self.assertNotIn("purgedFileCount", op)
        row = next(row for row in job_counters(self.gateway.sources(), self.store.data["operations"])["indexers"] if row["active"])
        self.assertIsNone(row["lastPurge"]["purgedCount"])

    def finish_sync(self, end, processed):
        for status in self.gateway.statuses.values():
            status["lastResult"].update({"endTime": end, "itemsProcessed": processed})

    def admin_row(self):
        return next(row for row in job_counters(self.gateway.sources(), self.store.data["operations"],
                                                self.store.data.get("syncFiles"))["indexers"]
                    if row["active"] and row["sourceKey"] == "admin-share")

    def test_sync_after_the_purge_updates_indexed_and_lists_only_changed_files(self):
        self.listing = {"admin-share": ["a.txt", "b.txt"], "alluser-share": []}
        purge = self.purge()
        self.source_files.side_effect = lambda row: [
            {**file(path, row["share"]), "modified": "2099-01-01T00:00:00+00:00" if path == "c.txt" else "2026-01-01T00:00:00+00:00"}
            for path in self.listing.get(row["share"], [])]
        self.finish_sync("2099-01-01T00:00:00Z", 1)
        self.listing["admin-share"].append("c.txt")
        sync_files.record(self.gateway, self.store)
        row = self.admin_row()
        self.assertEqual(row["indexed"], {"count": 3, "at": "2099-01-01T00:00:00Z", "from": "sync", "listed": True})
        self.assertTrue(row["lastSyncListed"])
        self.assertEqual(row["lastSyncCount"], 1)
        sources, ops, snaps = self.gateway.sources(), self.store.data["operations"], self.store.data["syncFiles"]
        self.assertEqual(job_files(sources, ops, snaps, "admin-share", "lastSync")[0], ["c.txt"])
        self.assertEqual(job_files(sources, ops, snaps, "admin-share", "indexed")[0], ["a.txt", "b.txt", "c.txt"])
        calls = self.source_files.call_count
        sync_files.record(self.gateway, self.store)
        self.assertEqual(self.source_files.call_count, calls, "an already recorded run must not list the share again")
        # A run that processed files was missed; the next empty run still brings INDEXED up to date.
        self.listing["admin-share"].append("e.txt")
        self.finish_sync("2099-01-02T00:00:00Z", 0)
        sync_files.record(self.gateway, self.store)
        self.assertTrue(self.admin_row()["lastSyncListed"])
        self.assertEqual(self.admin_row()["indexed"]["count"], 4)
        self.assertEqual(job_files(self.gateway.sources(), ops, self.store.data["syncFiles"], "admin-share", "lastSync"),
                         ([], "2099-01-02T00:00:00Z"))
        self.listing["admin-share"].append("d.txt")
        self.finish_sync("2099-01-03T00:00:00Z", 20)
        sync_files.record(self.gateway, self.store)
        self.assertEqual(job_files(self.gateway.sources(), ops, self.store.data["syncFiles"], "admin-share", "lastSync")[0],
                         ["d.txt"])
        self.assertEqual(self.admin_row()["lastSyncCount"], 1, "the counter matches the listed files, not Azure's itemsProcessed")
        self.assertNotEqual(purge["switchedAt"], self.admin_row()["indexed"]["at"])

    def test_purge_newer_than_the_sync_listing_wins(self):
        self.store.data["syncFiles"] = {"admin-share": {"endTime": "2000-01-01T00:00:00Z", "files": [], "changed": []}}
        self.listing = {"admin-share": ["a.txt"], "alluser-share": []}
        self.purge()
        row = self.admin_row()
        self.assertEqual((row["indexed"]["from"], row["indexed"]["count"]), ("purge", row["lastPurge"]["indexedCount"]))
        with self.assertRaises(ValueError):
            job_files(self.gateway.sources(), [], {}, "unknown", "indexed")

    def test_no_completed_purge_means_unknown_counters(self):
        row = job_counters(self.gateway.sources(), [{"id": "x", "type": "rebuild", "status": "running"}])["indexers"][0]
        self.assertEqual(row["lastPurge"], {"at": None, "indexedCount": None, "purgedCount": None})

    def test_standby_indexer_counts_the_purge_that_built_its_slot(self):
        self.listing = {"admin-share": ["a.txt", "b.txt"], "alluser-share": []}
        first = self.purge()
        self.listing = {"admin-share": ["a.txt"], "alluser-share": []}
        self.purge()
        sources, ops = self.gateway.sources(), self.store.data["operations"]
        standby = next(row for row in job_counters(sources, ops)["indexers"]
                       if not row["active"] and row["sourceKey"] == "admin-share")
        self.assertEqual(standby["index"], first["targetIndex"])
        self.assertEqual(standby["indexed"], {"count": 2, "at": first["switchedAt"], "from": "purge", "listed": True})
        self.assertEqual(standby["lastPurge"]["purgedCount"], 1)
        self.assertEqual(job_files(sources, ops, {}, "admin-share", "indexed", standby=True)[0], ["a.txt", "b.txt"])
        self.assertEqual(job_files(sources, ops, {}, "admin-share", "purged", standby=True)[0], ["b.txt"])
        self.assertEqual(job_files(sources, ops, {}, "admin-share", "indexed")[0], ["a.txt"])


class DocumentWebTests(unittest.TestCase):
    setUp = test_console.WebTests.setUp

    def test_sources_expose_last_job_counters_without_listing_files(self):
        self.store.data["operations"] = [{"id": str(uuid4()), "type": "rebuild", "status": "completed",
                                         "switched": True, "switchedAt": "2026-11-12T08:06:00+00:00",
                                         "itemsProcessed": {"admin": 7}, "purgedFiles": [file("gone.txt")]}]
        with patch("documents.source_files") as listing:
            sources = self.client.get("/api/sources", headers=self.headers).json
            listing.assert_not_called()
        row = next(row for row in sources["indexers"] if row["active"] and row["source"] == "af-ds-admin")
        self.assertEqual(row["lastPurge"], {"at": "2026-11-12T08:06:00+00:00", "indexedCount": 7, "purgedCount": 1})
        self.assertEqual(self.client.get("/api/documents", headers=self.headers).status_code, 404)
    def test_state_excludes_private_inventory_lists_but_not_counters(self):
        self.store.data["documents"] = {"admin-share": {"paths": ["private.txt"]}}
        self.store.data["operations"] = [{"id": str(uuid4()), "type": "rebuild",
                                         "purgedFiles": [file("private.txt")], "purgedFileCount": 1}]
        response = self.client.get("/api/state", headers=self.headers)
        self.assertNotIn("private.txt", response.get_data(as_text=True))
        self.assertEqual(response.json["operations"][0]["purgedFileCount"], 1)

    def test_counter_file_lists_are_protected_paginated_and_conflict_when_not_recorded(self):
        self.store.data["syncFiles"] = {"admin-share": {"endTime": "2099-01-01T00:00:00Z",
                                                        "files": [{"path": "a.txt"}, {"path": "b.txt"}], "changed": []}}
        url = "/api/sources/files?sourceKey=admin-share&kind=indexed"
        self.assertEqual(self.client.get(url).status_code, 401)
        result = self.client.get(url + "&offset=1&limit=1", headers=self.headers)
        self.assertEqual((result.json["total"], result.json["files"][0]["path"]), (2, "b.txt"))
        self.assertNotIn("a.txt", self.client.get("/api/state", headers=self.headers).get_data(as_text=True))
        self.assertEqual(self.client.get("/api/sources/files?sourceKey=admin-share&kind=purged",
                                         headers=self.headers).status_code, 409)
        self.assertEqual(self.client.get("/api/sources/files?sourceKey=admin-share&kind=bogus",
                                         headers=self.headers).status_code, 400)

    def test_purge_details_are_protected_paginated_and_not_fabricated_for_old_records(self):
        operation_id = str(uuid4())
        self.store.data["operations"] = [{"id": operation_id, "type": "rebuild", "switched": True,
                                         "purgedFiles": [file("one.txt"), file("two.txt")]}]
        url = f"/api/operations/{operation_id}/files"
        self.assertEqual(self.client.get(url).status_code, 401)
        result = self.client.get(url + "?offset=1&limit=1", headers=self.headers)
        self.assertEqual(result.json["total"], 2)
        self.assertEqual(result.json["files"][0]["path"], "two.txt")
        self.store.data["operations"][0].pop("purgedFiles")
        self.assertEqual(self.client.get(url, headers=self.headers).status_code, 409)


if __name__ == "__main__":
    unittest.main()
