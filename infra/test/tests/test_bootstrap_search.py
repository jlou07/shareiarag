from pathlib import Path
import sys
import unittest


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import bootstrap_search as bootstrap
import testenv


ENV = testenv.TestEnv(suffix="abc123", resource_group="rg-example")


GROUP_A = "11111111-2222-4333-8444-555555555555"
GROUP_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
CONNECTION = (
    f"DefaultEndpointsProtocol=https;AccountName={ENV.files};"
    "AccountKey=synthetic-key;EndpointSuffix=core.windows.net"
)


class SearchBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.source = bootstrap.SOURCE.read_text(encoding="utf-8")

    def test_builds_isolated_group_filtered_definitions(self):
        requests = dict(bootstrap.render_requests(ENV, self.source, GROUP_A, GROUP_B, CONNECTION))
        self.assertEqual(len(requests), 9)
        self.assertEqual(requests["/datasources/af-ds-admin"]["container"]["name"], "admin-share")
        self.assertEqual(requests["/datasources/af-ds-alluser"]["container"]["name"], "alluser-share")
        self.assertEqual(requests["/knowledgebases/af-kb-all"]["retrievalReasoningEffort"]["kind"], "medium")
        rendered = str(requests)
        self.assertIn(GROUP_A, rendered)
        self.assertIn(GROUP_B, rendered)
        self.assertIn(ENV.foundry_endpoint, rendered)
        self.assertNotIn("https://aif-shareiarag.openai.azure.com", rendered)
        self.assertNotIn("shareiarag.file.core.windows.net", rendered)

    def test_refuses_wrong_storage_or_non_distinct_groups(self):
        with self.assertRaisesRegex(ValueError, "isolated test storage"):
            bootstrap.render_requests(ENV, self.source, GROUP_A, GROUP_B, CONNECTION.replace(ENV.files, "shareiarag"))
        with self.assertRaisesRegex(ValueError, "distinct"):
            bootstrap.render_requests(ENV, self.source, GROUP_A, GROUP_A, CONNECTION)
        with self.assertRaises(ValueError):
            bootstrap.render_requests(ENV, self.source, "not-a-group", GROUP_B, CONNECTION)

    def test_refuses_changed_permissions_and_unexpected_targets(self):
        cases = (
            self.source.replace('"permissionFilterOption": "enabled"', '"permissionFilterOption": "disabled"'),
            self.source.replace('"source": "/document/group_ids"', '"source": "/document/other_groups"'),
            self.source.replace("{{$dotenv SEARCH_ENDPOINT}}/datasources/af-ds-admin", "https://example.test/datasources/af-ds-admin"),
        )
        for source in cases:
            with self.subTest(source=source[:60]), self.assertRaises(ValueError):
                bootstrap.render_requests(ENV, source, GROUP_A, GROUP_B, CONNECTION)


if __name__ == "__main__":
    unittest.main()
