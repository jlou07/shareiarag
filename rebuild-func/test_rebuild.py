import copy
import unittest

import rebuild
from rebuild import RebuildError, Rebuilder


def index(name="af-index-all"):
    return {
        "@odata.etag": "e",
        "name": name,
        "permissionFilterOption": "enabled",
        "fields": [{"name": "chunk_id", "key": True},
                   {"name": "group_ids", "permissionFilter": "groupIds"}],
        "vectorSearch": {"vectorizers": [{"azureOpenAIParameters": {"apiKey": "secret", "resourceUri": "u"}}]},
    }


def skillset(name, target):
    return {
        "name": name,
        "skills": [{"@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill", "apiKey": "secret"}],
        "indexProjections": {"selectors": [{
            "targetIndexName": target,
            "mappings": [{"name": "group_ids", "source": "/document/group_ids"}],
        }]},
    }


class FakeApi:
    def __init__(self, new_chunks=60, anonymous_hits=0, indexer_status="success"):
        self.objects = {
            "/knowledgesources/af-ks-all": {"@odata.etag": "k", "name": "af-ks-all",
                                            "searchIndexParameters": {"searchIndexName": "af-index-all"}},
            "/indexes/af-index-all": index(),
        }
        for share in rebuild.SHARES:
            self.objects[f"/skillsets/af-skills-all-{share}"] = skillset(f"af-skills-all-{share}", "af-index-all")
            self.objects[f"/indexers/af-indexer-all-{share}"] = {
                "@odata.etag": "i", "name": f"af-indexer-all-{share}", "dataSourceName": f"af-ds-{share}",
                "targetIndexName": "af-index-all", "skillsetName": f"af-skills-all-{share}",
                "schedule": {"interval": "PT5M"},
            }
        self.new_chunks = new_chunks
        self.anonymous_hits = anonymous_hits
        self.indexer_status = indexer_status
        self.runs = {}
        self.calls = []

    def get(self, path, missing_ok=False):
        return self.call("GET", path, expected=(200, 404) if missing_ok else (200,))

    def call(self, method, path, body=None, headers=None, expected=(200,)):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/status"):
            name = path.split("/")[2]
            n = self.runs.get(name, 0)
            return {"lastResult": {"status": self.indexer_status if n else "success", "startTime": str(n),
                                   "itemsProcessed": 20, "itemsFailed": 0}}
        if method == "GET" and path.endswith("/stats"):
            return {"documentCount": 64 if path.startswith("/indexes/af-index-all/") else self.new_chunks}
        if method == "GET":
            if path not in self.objects:
                if 404 in expected:
                    return None
                raise RebuildError("404 " + path)
            result = copy.deepcopy(self.objects[path])
            result.setdefault("@odata.etag", "x")
            return result
        if method == "PUT":
            self.objects[path] = copy.deepcopy(body)
            return None
        if method == "DELETE":
            self.objects.pop(path, None)
            return None
        if method == "POST" and path.endswith("/run"):
            name = path.split("/")[2]
            self.runs[name] = self.runs.get(name, 0) + 1
            return None
        if method == "POST" and path.endswith("/docs/search"):
            return {"@odata.count": self.anonymous_hits, "value": [{}] * self.anonymous_hits}
        return None


def rebuilder(api):
    return Rebuilder(api, sleep=lambda _: None, poll_seconds=0)


class RebuildTests(unittest.TestCase):
    def test_first_run_builds_slot_b_and_moves_schedule(self):
        api = FakeApi()
        report = rebuilder(api).run()
        self.assertTrue(report.switched)
        self.assertEqual(report.new_index, "af-index-all-b")
        self.assertEqual(api.objects["/knowledgesources/af-ks-all"]["searchIndexParameters"]["searchIndexName"],
                         "af-index-all-b")
        for share in rebuild.SHARES:
            self.assertEqual(api.objects[f"/indexers/af-indexer-all-b-{share}"]["schedule"]["interval"], "PT5M")
            self.assertNotIn("schedule", api.objects[f"/indexers/af-indexer-all-{share}"])
        new_index = api.objects["/indexes/af-index-all-b"]
        self.assertNotIn("apiKey", new_index["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"])
        self.assertEqual(new_index["permissionFilterOption"], "enabled")
        sk = api.objects["/skillsets/af-skills-all-b-admin"]
        self.assertNotIn("apiKey", sk["skills"][0])
        self.assertEqual(sk["indexProjections"]["selectors"][0]["targetIndexName"], "af-index-all-b")

    def test_second_run_rebuilds_slot_a(self):
        api = FakeApi()
        rebuilder(api).run()
        api.objects["/indexes/af-index-all-b"]["@odata.etag"] = "e"
        report = rebuilder(api).run()
        self.assertEqual(report.new_index, "af-index-all")
        self.assertIn(("DELETE", "/indexes/af-index-all"), api.calls)

    def test_large_drop_refuses_switch(self):
        api = FakeApi(new_chunks=10)
        with self.assertRaisesRegex(RebuildError, "Refusing to switch"):
            rebuilder(api).run()
        self.assertEqual(api.objects["/knowledgesources/af-ks-all"]["searchIndexParameters"]["searchIndexName"],
                         "af-index-all")
        self.assertEqual(api.objects["/indexers/af-indexer-all-admin"]["schedule"]["interval"], "PT5M")

    def test_anonymous_results_refuse_switch(self):
        api = FakeApi(anonymous_hits=1)
        with self.assertRaisesRegex(RebuildError, "without a user token"):
            rebuilder(api).run()
        self.assertEqual(api.objects["/knowledgesources/af-ks-all"]["searchIndexParameters"]["searchIndexName"],
                         "af-index-all")

    def test_failed_indexer_refuses_switch(self):
        api = FakeApi(indexer_status="transientFailure")
        with self.assertRaises(RebuildError):
            rebuilder(api).run()
        self.assertEqual(api.objects["/knowledgesources/af-ks-all"]["searchIndexParameters"]["searchIndexName"],
                         "af-index-all")

    def test_never_deletes_active_index(self):
        api = FakeApi()
        rebuilder(api).run()
        deletes = [p for m, p in api.calls if m == "DELETE"]
        self.assertEqual(deletes, ["/indexes/af-index-all-b"])

    def test_rejects_unfiltered_active_index(self):
        bad = index()
        bad["permissionFilterOption"] = "disabled"
        with self.assertRaises(RebuildError):
            rebuild.index_definition(bad, "x")

    def test_unexpected_knowledge_source_target(self):
        api = FakeApi()
        api.objects["/knowledgesources/af-ks-all"]["searchIndexParameters"]["searchIndexName"] = "other"
        with self.assertRaises(RebuildError):
            rebuilder(api).run()
        self.assertFalse([c for c in api.calls if c[0] == "DELETE"])


if __name__ == "__main__":
    unittest.main()
