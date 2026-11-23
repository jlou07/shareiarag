import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import seed_test_secrets as seed
import testenv


ENV = testenv.TestEnv(suffix="abc123", resource_group="rg-example",
                      download_client_id="11111111-2222-4333-8444-555555555555")


class SeedSecretsTests(unittest.TestCase):
    def test_existing_files_secret_is_never_overwritten(self):
        with patch.object(seed.test, "verify_test_scope", return_value=("sub", "44444444-5555-4666-8777-888888888888")), \
                patch.object(seed, "_token", return_value="token"), \
                patch.object(seed, "_request", return_value=200) as request, \
                patch.object(seed.test, "_az") as az:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                seed.seed_files_secret(ENV)
        az.assert_not_called()
        self.assertEqual(request.call_count, 1)

    def test_files_key_is_not_passed_on_command_line(self):
        key = "synthetic-storage-key"
        with patch.object(seed.test, "verify_test_scope", return_value=("sub", "44444444-5555-4666-8777-888888888888")), \
                patch.object(seed, "_token", return_value="token"), \
                patch.object(seed, "_request", side_effect=[404, 200]) as request, \
                patch.object(seed.test, "_az", return_value=key) as az:
            seed.seed_files_secret(ENV)
        self.assertNotIn(key, str(az.call_args))
        self.assertEqual(request.call_args.args[0], "PUT")
        self.assertIn(f"AccountKey={key}", request.call_args.args[3]["value"])

    def test_unexpected_secret_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            seed._vault_url(ENV, "not-a-test-secret")

    def test_files_write_timeout_reports_uncertain_secret(self):
        with patch.object(seed.test, "verify_test_scope", return_value=("sub", "44444444-5555-4666-8777-888888888888")), \
                patch.object(seed, "_token", return_value="token"), \
                patch.object(seed, "_request", side_effect=[404, RuntimeError("timeout")]), \
                patch.object(seed.test, "_az", return_value="synthetic-key"):
            with self.assertRaisesRegex(RuntimeError, "write may have succeeded"):
                seed.seed_files_secret(ENV)

    def test_download_credential_is_removed_if_vault_write_fails(self):
        client_id = "11111111-2222-4333-8444-555555555555"
        app_id = "22222222-3333-4444-8555-666666666666"
        credential_id = "33333333-4444-4555-8666-777777777777"
        app = json.dumps({
            "id": app_id, "appId": client_id, "displayName": ENV.download_app, "passwordNames": [],
        })
        responses = [404, {"keyId": credential_id, "secretText": "synthetic-secret"}, RuntimeError("vault"), 204]
        with patch.object(seed.test, "verify_test_scope", return_value=("sub", "44444444-5555-4666-8777-888888888888")), \
                patch.object(seed.test, "_az", side_effect=[app, client_id]), \
                patch.object(seed, "_token", side_effect=["vault-token", "graph-token"]), \
                patch.object(seed, "_request", side_effect=responses) as request:
            with self.assertRaisesRegex(RuntimeError, "credential was removed"):
                seed.seed_download_secret(ENV)
        self.assertTrue(request.call_args.args[1].endswith("/removePassword"))
        self.assertEqual(request.call_args.args[3], {"keyId": credential_id})

    def test_download_password_timeout_reports_uncertain_credential(self):
        client_id = "11111111-2222-4333-8444-555555555555"
        app_id = "22222222-3333-4444-8555-666666666666"
        app = json.dumps({
            "id": app_id, "appId": client_id, "displayName": ENV.download_app, "passwordNames": [],
        })
        with patch.object(seed.test, "verify_test_scope", return_value=("sub", "44444444-5555-4666-8777-888888888888")), \
                patch.object(seed.test, "_az", side_effect=[app, client_id]), \
                patch.object(seed, "_token", side_effect=["vault-token", "graph-token"]), \
                patch.object(seed, "_request", side_effect=[404, RuntimeError("timeout")]):
            with self.assertRaisesRegex(RuntimeError, "Graph may have created credential"):
                seed.seed_download_secret(ENV)

    def test_download_secret_refuses_a_different_application(self):
        client_id = "11111111-2222-4333-8444-555555555555"
        app = json.dumps({"id": client_id, "appId": client_id, "displayName": "not-the-test-app"})
        with patch.object(seed.test, "verify_test_scope", return_value=("sub", "44444444-5555-4666-8777-888888888888")), \
                patch.object(seed.test, "_az", return_value=app), \
                patch.object(seed, "_request") as request:
            with self.assertRaisesRegex(RuntimeError, "expected test"):
                seed.seed_download_secret(ENV)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
