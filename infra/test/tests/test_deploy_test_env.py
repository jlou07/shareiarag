from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


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


if __name__ == "__main__":
    unittest.main()
