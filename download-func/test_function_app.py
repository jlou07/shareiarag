import base64
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import azure.functions as func

with patch.dict(os.environ, {
    "FILES_ACCOUNT_NAME": "testfiles",
    "FILES_ACCOUNT_KEY": "unused-in-tests",
    "SHARE_GROUPS": "admin-share=admin-group;alluser-share=alluser-group",
}):
    import function_app

TENANT_ID = "12345678-1234-4234-8234-123456789012"
CLIENT_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


class OpenFileTests(unittest.TestCase):
    def setUp(self):
        auth = patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": "True"})
        auth.start()
        self.addCleanup(auth.stop)
        for name, value in (("TRUSTED_TENANT_ID", TENANT_ID), ("TRUSTED_AUDIENCES", {CLIENT_ID, f"api://{CLIENT_ID}"})):
            pinned = patch.object(function_app, name, value)
            pinned.start()
            self.addCleanup(pinned.stop)

    def request(self, path, groups=(), params=None, tenant=TENANT_ID):
        principal = {"claims": [{"typ": "groups", "val": group} for group in groups] + [
            {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tenant},
            {"typ": "aud", "val": CLIENT_ID},
        ]}
        encoded = base64.b64encode(json.dumps(principal).encode()).decode()
        params = {"relogin": "1"} if params is None else params
        return func.HttpRequest(
            method="GET",
            url="https://example.test/api/f/" + path,
            headers={"x-ms-client-principal": encoded},
            route_params={"path": path},
            params=params,
            body=b"",
        )

    def test_stale_session_signs_in_again_once_before_denying(self):
        with patch.object(function_app, "generate_file_sas") as generate:
            response = function_app.open_file(
                self.request("alluser-share/file.pdf", ("admin-group",), params={"mode": "view"})
            )
        generate.assert_not_called()
        self.assertEqual(response.status_code, 302)
        login = urlsplit(response.headers["Location"])
        self.assertEqual(login.path, "/.auth/login/aad")
        self.assertEqual(parse_qs(login.query)["post_login_redirect_uri"],
                         ["/api/f/alluser-share/file.pdf?mode=view&relogin=1"])

    def test_invalid_path_uses_english_message(self):
        response = function_app.open_file(self.request("admin-share/../file.pdf"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_body(), b"Invalid path.")

    def test_unknown_share_uses_english_message(self):
        response = function_app.open_file(self.request("unknown/file.pdf"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_body(), b"Unknown share.")

    def test_wrong_group_cannot_generate_a_file_link(self):
        with patch.object(function_app, "generate_file_sas") as generate:
            response = function_app.open_file(
                self.request("alluser-share/file.pdf", ("admin-group",))
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_body(), b"Access to this share is denied.")
        generate.assert_not_called()
        self.assertNotIn("Location", response.headers)

    def test_missing_identity_is_denied(self):
        request = func.HttpRequest(
            method="GET", url="https://example.test", body=b"",
            route_params={"path": "admin-share/file.pdf"},
        )
        response = function_app.open_file(request)
        self.assertEqual(response.status_code, 403)

    def test_forged_principal_is_rejected_when_platform_auth_is_off(self):
        with patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": "False"}), \
                patch.object(function_app, "generate_file_sas") as generate:
            response = function_app.open_file(self.request("admin-share/report.pdf", ("admin-group",)))
        self.assertEqual(response.status_code, 503)
        generate.assert_not_called()

    def test_principal_from_another_tenant_gets_no_groups(self):
        with patch.object(function_app, "generate_file_sas") as generate:
            response = function_app.open_file(self.request(
                "admin-share/report.pdf", ("admin-group",), tenant="00000000-0000-4000-8000-000000000000"))
        self.assertIn(response.status_code, (302, 403))
        self.assertNotIn("core.windows.net", response.headers.get("Location", ""))
        generate.assert_not_called()

    def test_active_content_is_downloaded_not_rendered(self):
        for name in ("page.html", "logo.svg", "script.js"):
            with self.subTest(name=name), \
                    patch.object(function_app, "generate_file_sas", return_value="test-sas") as generate:
                function_app.open_file(self.request(f"admin-share/{name}", ("admin-group",)))
                self.assertEqual(generate.call_args.kwargs["content_type"], "application/octet-stream")
                self.assertTrue(generate.call_args.kwargs["content_disposition"].startswith("attachment;"))

    def test_pdf_remains_inline_for_the_allowed_group(self):
        with patch.object(function_app, "generate_file_sas", return_value="test-sas") as generate:
            response = function_app.open_file(
                self.request("admin-share/report.pdf", ("admin-group",))
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "https://testfiles.file.core.windows.net/admin-share/report.pdf?test-sas",
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(generate.call_args.kwargs["content_type"], "application/pdf")
        self.assertTrue(generate.call_args.kwargs["content_disposition"].startswith("inline;"))

    def test_office_viewer_is_preserved_in_view_mode(self):
        request = self.request("alluser-share/report.docx", ("alluser-group",), params={"mode": "view"})
        request = func.HttpRequest(method="GET", url=request.url + "?mode=view", headers=dict(request.headers),
                                   route_params=request.route_params, params={"mode": "view"}, body=b"")
        with patch.object(function_app, "generate_file_sas", return_value="test-sas") as generate:
            response = function_app.open_file(request)
        expiry = generate.call_args.kwargs["expiry"] - function_app.datetime.now(function_app.timezone.utc)
        self.assertLessEqual(expiry.total_seconds(), function_app.OFFICE_VIEWER_SAS_MINUTES * 60)
        redirect = urlsplit(response.headers["Location"])
        self.assertEqual(redirect.netloc, "view.officeapps.live.com")
        self.assertEqual(
            parse_qs(redirect.query)["src"],
            ["https://testfiles.file.core.windows.net/alluser-share/report.docx?test-sas"],
        )

    def test_office_file_offers_desktop_edit_over_smb_and_online_view(self):
        with patch.object(function_app, "generate_file_sas") as generate:
            response = function_app.open_file(
                self.request("alluser-share/My Folder/budget.xlsx", ("alluser-group",))
            )
        generate.assert_not_called()
        body = response.get_body().decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'href="ms-excel:ofe|u|file://testfiles.file.core.windows.net/alluser-share/My%20Folder/budget.xlsx"', body
        )
        self.assertIn(r"\\testfiles.file.core.windows.net\alluser-share\My Folder\budget.xlsx", body)
        self.assertIn('href="?mode=view" target="_blank" rel="noopener"', body)
        self.assertNotIn("<script", body, "the desktop app must open only when the user chooses it")

    def test_account_qualified_link_uses_only_that_accounts_group_and_key(self):
        with patch.dict(function_app.SHARE_GROUPS, {"otherfiles/admin-share": "other-group"}), \
                patch.dict(os.environ, {"FILES_ACCOUNT_KEY_OTHERFILES": "other-key"}), \
                patch.object(function_app, "generate_file_sas", return_value="test-sas") as generate:
            denied = function_app.open_file(self.request("accounts/otherfiles/admin-share/report.pdf", ("admin-group",)))
            generate.assert_not_called()
            self.assertEqual(denied.status_code, 403)
            allowed = function_app.open_file(self.request("accounts/otherfiles/admin-share/report.pdf", ("other-group",)))
        self.assertEqual(allowed.status_code, 302)
        self.assertEqual(generate.call_args.kwargs["account_name"], "otherfiles")
        self.assertEqual(generate.call_args.kwargs["account_key"], "other-key")
        self.assertEqual(allowed.headers["Location"], "https://otherfiles.file.core.windows.net/admin-share/report.pdf?test-sas")

    def test_key_vault_connection_string_is_accepted_only_for_its_account(self):
        connection = "DefaultEndpointsProtocol=https;AccountName=otherfiles;AccountKey=vault-key;EndpointSuffix=core.windows.net"
        with patch.dict(function_app.SHARE_GROUPS, {"otherfiles/admin-share": "other-group"}), \
                patch.dict(os.environ, {"FILES_ACCOUNT_KEY_OTHERFILES": connection}), \
                patch.object(function_app, "generate_file_sas", return_value="test-sas") as generate:
            allowed = function_app.open_file(self.request("accounts/otherfiles/admin-share/report.pdf", ("other-group",)))
        self.assertEqual(allowed.status_code, 302)
        self.assertEqual(generate.call_args.kwargs["account_key"], "vault-key")
        self.assertIsNone(function_app._account_key(connection.replace("otherfiles", "wrongfiles"), "otherfiles"))

    def test_qualified_link_never_falls_back_to_legacy_share_permissions(self):
        with patch.object(function_app, "generate_file_sas") as generate:
            result = function_app.open_file(self.request("accounts/otherfiles/admin-share/report.pdf", ("admin-group",)))
        self.assertEqual(result.status_code, 404)
        generate.assert_not_called()

    def test_missing_account_key_is_explicit_unavailability_not_default_account(self):
        with patch.dict(function_app.SHARE_GROUPS, {"otherfiles/admin-share": "other-group"}), \
                patch.dict(os.environ, {"FILES_ACCOUNT_KEY_OTHERFILES": ""}), \
                patch.object(function_app, "generate_file_sas") as generate:
            result = function_app.open_file(self.request("accounts/otherfiles/admin-share/report.pdf", ("other-group",)))
        self.assertEqual(result.status_code, 503)
        generate.assert_not_called()

    def test_short_link_resolves_console_source_on_default_account_only(self):
        with patch.dict(function_app.SHARE_GROUPS, {"testfiles/blogshare": "blog-group", "otherfiles/othershare": "other-group"}), \
                patch.object(function_app, "generate_file_sas", return_value="test-sas") as generate:
            denied = function_app.open_file(self.request("blogshare/receipt.pdf", ("admin-group",)))
            self.assertEqual(denied.status_code, 403)
            generate.assert_not_called()
            other = function_app.open_file(self.request("othershare/receipt.pdf", ("other-group",)))
            self.assertEqual(other.status_code, 404)
            generate.assert_not_called()
            allowed = function_app.open_file(self.request("blogshare/receipt.pdf", ("blog-group",)))
        self.assertEqual(allowed.status_code, 302)
        self.assertEqual(generate.call_args.kwargs["account_name"], "testfiles")
        self.assertEqual(allowed.headers["Location"], "https://testfiles.file.core.windows.net/blogshare/receipt.pdf?test-sas")

    def test_invalid_qualified_account_and_traversal_are_rejected(self):
        for path in ("accounts/evil.example/admin-share/file.pdf", "accounts/otherfiles/admin-share",
                     "accounts/otherfiles/admin-share/%2e%2e/file.pdf"):
            with self.subTest(path=path):
                result = function_app.open_file(self.request(path, ("admin-group",)))
                self.assertEqual(result.status_code, 400)


if __name__ == "__main__":
    unittest.main()
