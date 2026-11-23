from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import create_entra_apps as entra
import testenv


class CreateEntraAppsTests(unittest.TestCase):
    def test_preauthorize_replaces_existing_entry_and_keeps_others(self):
        api = {"requestedAccessTokenVersion": 2, "preAuthorizedApplications": [
            {"appId": "other", "delegatedPermissionIds": ["s1"]},
            {"appId": "client", "delegatedPermissionIds": ["old"]},
        ]}
        result = entra.preauthorize(api, "client", "s2")
        self.assertEqual(result["requestedAccessTokenVersion"], 2)
        self.assertEqual(result["preAuthorizedApplications"], [
            {"appId": "other", "delegatedPermissionIds": ["s1"]},
            {"appId": "client", "delegatedPermissionIds": ["s2"]},
        ])

    def test_bodies_follow_environment_names_and_wiring(self):
        env = testenv.TestEnv(suffix="abc123", resource_group="rg-example")
        console = entra.console_app_body(env, "c-scope", "mcp-id", "m-scope")
        self.assertEqual(console["displayName"], "ent-shareiarag-test-console-abc123")
        self.assertIn({"resourceAppId": "mcp-id", "resourceAccess": [{"id": "m-scope", "type": "Scope"}]},
                      console["requiredResourceAccess"])
        download = entra.download_app_body(env)
        self.assertTrue(download["web"]["implicitGrantSettings"]["enableIdTokenIssuance"])
        credential = entra.federated_credential_body(env, "tenant", "principal")
        self.assertEqual(credential["issuer"], "https://login.microsoftonline.com/tenant/v2.0")


if __name__ == "__main__":
    unittest.main()
