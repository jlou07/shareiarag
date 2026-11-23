import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import testenv


class TestEnvTests(unittest.TestCase):
    def test_python_names_match_bicep_naming_convention(self):
        bicep = (TEST_DIR / "main.bicep").read_text(encoding="utf-8")
        templates = dict(re.findall(r"(?m)^var (\w+Name) = '([^']+)'$", bicep))
        env = testenv.TestEnv(suffix="abc123", resource_group="rg-example")
        expected = {
            "searchName": env.search, "foundryAccountName": env.foundry, "foundryProjectName": env.project,
            "filesAccountName": env.files, "vaultName": env.vault, "consoleAppName": env.console_site,
            "mcpAppName": env.mcp_site, "downloadAppName": env.download_site, "mcpIdentityName": env.mcp_identity,
        }
        for variable, name in expected.items():
            with self.subTest(variable=variable):
                self.assertEqual(templates[variable].replace("${suffix}", env.suffix), name)

    def test_loads_parameters_file_and_rejects_placeholders(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "params.json"
            path.write_text(json.dumps({"parameters": {
                "suffix": {"value": "xyz9"}, "resourceGroupName": {"value": "rg-x"},
                "testUserObjectIds": {"value": ["A", "B"]},
            }}), encoding="utf-8")
            env = testenv.load(path)
            self.assertEqual((env.search, env.resource_group, env.test_users),
                             ("srch-shareiarag-test-xyz9", "rg-x", ("a", "b")))
            example = json.loads((TEST_DIR / "runtime.parameters.example.json").read_text(encoding="utf-8"))
            path.write_text(json.dumps(example), encoding="utf-8")
            with self.assertRaises(ValueError):
                testenv.load(path)
        for suffix in ("ab", "ABC1", "abc-1", "abcdefghi"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                testenv.TestEnv(suffix=suffix, resource_group="rg-x")


if __name__ == "__main__":
    unittest.main()
