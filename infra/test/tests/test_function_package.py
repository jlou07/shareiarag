import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import azure.functions as func


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import build_function_package as package


TEST_CLIENT = "11111111-2222-4333-8444-555555555555"
TEST_TENANT = "99999999-8888-4777-8666-555555555555"
OTHER_TENANT = "00000000-0000-4000-8000-000000000000"


class FunctionPackageTests(unittest.TestCase):
    def setUp(self):
        self.source = (package.SOURCE_DIR / "function_app.py").read_text(encoding="utf-8")

    def test_real_identities_in_source_and_invalid_inputs_are_rejected(self):
        tree = package.ast.parse(self.source)
        placeholder = "00000000-0000-0000-0000-000000000000"
        with self.assertRaisesRegex(ValueError, "placeholders"):
            committed = self.source.replace(f'TRUSTED_TENANT_ID = "{placeholder}"', f'TRUSTED_TENANT_ID = "{TEST_TENANT}"')
            package.pin_test_identity(committed, TEST_CLIENT, TEST_TENANT)
        with self.assertRaisesRegex(ValueError, "placeholders"):
            committed = self.source.replace(f'{{"{placeholder}", "api://{placeholder}"}}', f'{{"{TEST_CLIENT}", "api://{TEST_CLIENT}"}}')
            package.pin_test_identity(committed, TEST_CLIENT, TEST_TENANT)
        with self.assertRaises(ValueError):
            package.pin_test_identity(self.source, "not-a-uuid", TEST_TENANT)
        with self.assertRaisesRegex(ValueError, "zero UUID"):
            package.pin_test_identity(self.source, "00000000-0000-0000-0000-000000000000", TEST_TENANT)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            package.pin_test_identity(self.source + "\nTRUSTED_AUDIENCES = set()\n", TEST_CLIENT, TEST_TENANT)
        with self.assertRaisesRegex(ValueError, "review"):
            original_tenant = package.ast.literal_eval(package._assignment(tree, "TRUSTED_TENANT_ID").value)
            package.pin_test_identity(self.source.replace(original_tenant, "not-a-tenant"), TEST_CLIENT, TEST_TENANT)

    def test_package_is_separate_and_refuses_overwrite(self):
        before = (package.SOURCE_DIR / "function_app.py").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "function.zip"
            source_hash, result_hash = package.build_package(TEST_CLIENT, TEST_TENANT, output)
            self.assertEqual(len(source_hash), 64)
            self.assertEqual(len(result_hash), 64)
            with ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {"function_app.py", "host.json", "requirements.txt"})
                for entry in archive.infolist():
                    self.assertEqual(entry.create_system, 3)
                    self.assertEqual((entry.external_attr >> 16) & 0o777, 0o644)
                pinned = archive.read("function_app.py").decode("utf-8")
            self.assertIn(f'"api://{TEST_CLIENT}"', pinned)
            self.assertIn(f'TRUSTED_TENANT_ID = "{TEST_TENANT}"', pinned)
            with self.assertRaises(FileExistsError):
                package.build_package(TEST_CLIENT, TEST_TENANT, output)
            with self.assertRaisesRegex(ValueError, "outside"):
                package.build_package(TEST_CLIENT, TEST_TENANT, package.SOURCE_DIR / "not-created.zip")
        self.assertEqual((package.SOURCE_DIR / "function_app.py").read_bytes(), before)

    def test_test_audience_and_groups_are_enforced_by_packaged_function(self):
        source = package.pin_test_identity(self.source, TEST_CLIENT, TEST_TENANT)
        module = types.ModuleType("test_download_function")
        with patch.dict(os.environ, {
            "FILES_ACCOUNT_NAME": "testfiles",
            "FILES_ACCOUNT_KEY": "test-key",
            "SHARE_GROUPS": "admin-share=group-a;alluser-share=group-b",
            "WEBSITE_AUTH_ENABLED": "True",
        }):
            exec(compile(source, "function_app.py", "exec"), module.__dict__)

            def request(audience, group="group-a", tenant=TEST_TENANT):
                principal = {"claims": [
                    {"typ": "groups", "val": group},
                    {"typ": "tid", "val": tenant},
                    {"typ": "aud", "val": audience},
                ]}
                return func.HttpRequest(
                    method="GET",
                    url="https://example.test/api/f/admin-share/report.pdf",
                    headers={"x-ms-client-principal": base64.b64encode(json.dumps(principal).encode()).decode()},
                    route_params={"path": "admin-share/report.pdf"},
                    params={"relogin": "1"},
                    body=b"",
                )

            with patch.object(module, "generate_file_sas", return_value="test-sas") as generate:
                self.assertEqual(module.open_file(request(TEST_CLIENT)).status_code, 302)
                self.assertEqual(module.open_file(request(f"api://{TEST_CLIENT}")).status_code, 302)
                self.assertEqual(generate.call_count, 2)
                for bad_request in (
                    request("00000000-0000-4000-8000-000000000000"),
                    request(TEST_CLIENT, group="group-b"),
                    request(TEST_CLIENT, tenant=OTHER_TENANT),
                ):
                    self.assertEqual(module.open_file(bad_request).status_code, 403)
                self.assertEqual(generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
