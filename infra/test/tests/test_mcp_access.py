from pathlib import Path
import sys
import unittest

from mcp.types import CallToolResult, TextContent


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import testenv
import verify_mcp_access as access


class McpAccessTestTests(unittest.TestCase):
    def test_reads_structured_or_text_tool_results(self):
        payload = {"results": [{"id": "test-id"}]}
        self.assertEqual(access._success(CallToolResult(content=[], structuredContent=payload)), payload)
        self.assertEqual(access._success(CallToolResult(content=[], structuredContent={"result": payload})), payload)
        self.assertEqual(access._success(CallToolResult(content=[
            TextContent(type="text", text='{"results":[{"id":"test-id"}]}')
        ])), payload)

    def test_rejects_invalid_tool_results(self):
        with self.assertRaises(RuntimeError):
            access._success(CallToolResult(content=[TextContent(type="text", text="not JSON")]))
        with self.assertRaises(RuntimeError):
            access._success(CallToolResult(content=[], isError=True))

    def test_requires_actual_access_denial_not_any_error(self):
        denied = CallToolResult(isError=True, content=[
            TextContent(type="text", text="Error executing tool fetch: Document not found or not accessible to this user.")
        ])
        access._expect_denied(denied)
        with self.assertRaises(RuntimeError):
            access._expect_denied(CallToolResult(isError=True, content=[TextContent(type="text", text="Search unavailable")]))
        with self.assertRaises(RuntimeError):
            access._expect_denied(CallToolResult(isError=False, content=[]))

    def test_requires_allowed_file_and_rejects_cross_group_result(self):
        data = {"results": [{"id": "test-id", "title": "admin-test.txt", "text": "ALPHA-4827"}]}
        self.assertEqual(access._result_for(data, "admin-test.txt", "alluser-test.txt", "BRAVO-9631", True),
                         "test-id")
        with self.assertRaisesRegex(RuntimeError, "exposed"):
            access._result_for({"results": [{"title": "alluser-test.txt"}]},
                               "admin-test.txt", "alluser-test.txt", "BRAVO-9631", False)
        with self.assertRaisesRegex(RuntimeError, "not returned"):
            access._result_for({"results": []}, "admin-test.txt", "alluser-test.txt", "BRAVO-9631", True)

    def test_allowed_clients_accept_console_with_optional_m365_sso_only(self):
        console = "11111111-2222-4333-8444-555555555555"
        access.check_allowed_clients(console.upper(), console)
        access.check_allowed_clients(f"{console}, {testenv.M365_TOKEN_STORE_CLIENT_ID}", console)
        for setting in ("", "22222222-3333-4444-8555-666666666666",
                        f"{console},22222222-3333-4444-8555-666666666666"):
            with self.subTest(setting=setting), self.assertRaises(RuntimeError):
                access.check_allowed_clients(setting, console)


if __name__ == "__main__":
    unittest.main()
