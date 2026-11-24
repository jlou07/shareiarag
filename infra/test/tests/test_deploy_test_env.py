from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy_test_env as deploy


RG = "/subscriptions/s/resourceGroups/rg-x"


def what_if(*changes, status="Succeeded"):
    return {"status": status, "changes": [{"changeType": kind, "resourceId": rid} for kind, rid in changes]}


class WhatIfGuardTests(unittest.TestCase):
    def test_accepts_changes_inside_the_resource_group(self):
        deploy.check_what_if(what_if(("Create", RG), ("Modify", RG + "/providers/Microsoft.Web/sites/a")), "rg-x")

    def test_rejects_delete_other_group_and_failed_validation(self):
        cases = (
            what_if(("Delete", RG + "/providers/Microsoft.Web/sites/a")),
            what_if(("Create", "/subscriptions/s/resourceGroups/rg-x2/providers/Microsoft.Web/sites/a")),
            what_if(("Modify", "/subscriptions/s/providers/Microsoft.Authorization/roleDefinitions/r")),
            what_if(status="Failed"),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                deploy.check_what_if(case, "rg-x")

    def test_ignores_unchanged_resources_elsewhere(self):
        deploy.check_what_if(what_if(("NoChange", "/subscriptions/s/resourceGroups/other/x")), "rg-x")


class ParametersTests(unittest.TestCase):
    def test_init_fills_every_example_parameter_without_placeholders(self):
        args = SimpleNamespace(resource_group="rg-x", location="francecentral", admins=None,
                               tester_a="ta", tester_b="tb", group_a="ga", group_b="gb")
        parameters = deploy.build_parameters("abc1", args, "me")["parameters"]
        self.assertEqual(parameters["administratorObjectIds"]["value"], ["me"])
        self.assertEqual(parameters["testUserObjectIds"]["value"], ["ta", "tb"])
        for name, entry in parameters.items():
            with self.subTest(name=name):
                self.assertFalse(str(entry["value"]).startswith("<"))


class KeyVaultReferenceTests(unittest.TestCase):
    def test_refreshes_references_restarts_app_and_waits_until_resolved(self):
        response = {"value": [{"name": "FILES_CONNECTION_ACCOUNT",
                               "properties": {"status": "Resolved"}}]}
        with patch.object(deploy, "az") as az, patch.object(deploy, "az_json", return_value=response):
            deploy.refresh_key_vault_references("rg-x", "app-x", ("webapp", "restart"))

        calls = [call.args for call in az.call_args_list]
        self.assertIn(("webapp", "restart", "-g", "rg-x", "-n", "app-x"), calls)
        self.assertTrue(any("configreferences/appsettings/refresh?api-version=2022-03-01" in args
                            for call in calls for args in call))

    def test_refresh_fails_if_key_vault_references_do_not_resolve(self):
        response = {"value": [{"name": "FILES_CONNECTION_ACCOUNT",
                               "properties": {"status": "AccessToKeyVaultDenied"}}]}
        with patch.object(deploy, "az"), patch.object(deploy, "az_json", return_value=response), \
                patch.object(deploy.time, "monotonic", side_effect=[0, 601]):
            with self.assertRaisesRegex(TimeoutError, "AccessToKeyVaultDenied"):
                deploy.refresh_key_vault_references("rg-x", "app-x", ("webapp", "restart"))


if __name__ == "__main__":
    unittest.main()
