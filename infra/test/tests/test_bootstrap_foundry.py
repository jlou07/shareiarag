from pathlib import Path
import sys
import unittest


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import bootstrap_foundry as foundry
import testenv


ENV = testenv.TestEnv(suffix="abc123", resource_group="rg-example")


class FoundryBootstrapTests(unittest.TestCase):
    def test_agent_and_connection_target_only_the_isolated_services(self):
        host = f"https://{ENV.download_site}.azurewebsites.net/api/f"
        agent = foundry.agent_body(ENV, foundry.SOURCE.read_text(encoding="utf-8"), host)["definition"]
        connection = foundry.connection_body(ENV)["properties"]
        tool = agent["tools"][0]
        self.assertEqual(agent["model"], "gpt-5-mini")
        self.assertEqual(tool["server_url"], foundry.kb_endpoint(ENV))
        self.assertEqual(tool["project_connection_id"], foundry.CONNECTION)
        self.assertEqual(connection["target"], foundry.kb_endpoint(ENV))
        self.assertEqual(connection["authType"], "ProjectManagedIdentity")
        self.assertEqual(connection["audience"], "https://search.azure.com/")
        self.assertIn(host, agent["instructions"])
        self.assertNotIn("func-shareiarag-dl.azurewebsites.net", agent["instructions"])
        self.assertNotIn("shareiarag.file.core.windows.net", agent["instructions"])

    def test_agent_refuses_non_test_function_host(self):
        with self.assertRaises(ValueError):
            foundry.agent_body(ENV, foundry.SOURCE.read_text(encoding="utf-8"),
                               "https://func-shareiarag-dl.azurewebsites.net/api/f")


if __name__ == "__main__":
    unittest.main()
