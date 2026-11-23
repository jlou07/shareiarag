from pathlib import Path
import sys
import unittest


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import build_agent_instructions as builder
import testenv


ENV = testenv.TestEnv(suffix="abc123", resource_group="rg-example")

TEST_URL = f"https://{ENV.download_site}.azurewebsites.net/api/f"


class AgentInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.source = builder.SOURCE.read_text(encoding="utf-8")

    def test_only_test_file_links_are_rendered(self):
        result = builder.render_instructions(ENV, self.source, TEST_URL)
        self.assertIn(f"{ENV.files}/admin-share", result)
        self.assertIn(f"{TEST_URL}/accounts/<account>/<share>/<file-path>", result)
        self.assertIn(f"https://{ENV.files}.file.core.windows.net/", result)
        self.assertNotIn("https://func-shareiarag-dl.azurewebsites.net", result)
        self.assertNotIn("https://shareiarag.file.core.windows.net/", result)

    def test_refuses_unexpected_endpoint_or_changed_instructions(self):
        for endpoint in (
            f"http://{ENV.download_site}.azurewebsites.net/api/f",
            "https://func-shareiarag-test-dl-other1.azurewebsites.net/api/f",
            "https://func-shareiarag-dl.azurewebsites.net/api/f",
            f"{TEST_URL}?token=value",
            f"{TEST_URL}/other",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                builder.render_instructions(ENV, self.source, endpoint)
        with self.assertRaisesRegex(ValueError, "changed"):
            builder.render_instructions(ENV, self.source.replace("- For the original ", "- Updated "), TEST_URL)


if __name__ == "__main__":
    unittest.main()
