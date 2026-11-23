import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.update({
    "MCP_MODE": "hosted",
    "SEARCH_ENDPOINT": "https://example.search.windows.net",
    "MCP_PUBLIC_HOST": "testserver",
    "AZURE_TENANT_ID": "11111111-1111-4111-8111-111111111111",
    "MCP_CLIENT_ID": "22222222-2222-4222-8222-222222222222",
    "MCP_CLIENT_SECRET": "test-placeholder",
    "MCP_TOKEN_AUDIENCES": "22222222-2222-4222-8222-222222222222",
    "MCP_ALLOWED_CLIENT_IDS": "33333333-3333-4333-8333-333333333333",
    "MCP_REQUIRED_SCOPE": "MCP.Access",
})

import jwt
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

import cloud_app
import server


class HostedAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.client = cls.enterClassContext(TestClient(cloud_app.app))

    def assertion(self, **changes):
        now = int(time.time())
        claims = {
            "iss": cloud_app.ISSUER,
            "aud": cloud_app.CLIENT_ID,
            "tid": cloud_app.TENANT_ID,
            "oid": "44444444-4444-4444-8444-444444444444",
            "azp": "33333333-3333-4333-8333-333333333333",
            "scp": "MCP.Access",
            "iat": now,
            "nbf": now,
            "exp": now + 300,
        }
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256")

    def setUp(self):
        self.keys = patch.object(
            cloud_app.SIGNING_KEYS, "get_signing_key_from_jwt",
            return_value=SimpleNamespace(key=self.key.public_key()),
        )
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def test_missing_token_is_rejected(self):
        response = self.client.post("/mcp", headers={"Host": "testserver"})
        self.assertEqual(response.status_code, 401)

    def test_wrong_audience_and_client_do_not_trigger_delegation(self):
        with patch.object(cloud_app.msal, "ConfidentialClientApplication") as msal_app:
            with self.assertRaises(jwt.InvalidAudienceError):
                cloud_app._verify_and_exchange(self.assertion(aud="wrong-audience"))
            with self.assertRaises(cloud_app.ForbiddenToken):
                cloud_app._verify_and_exchange(
                    self.assertion(azp="55555555-5555-4555-8555-555555555555")
                )
            with self.assertRaises(cloud_app.ForbiddenToken):
                cloud_app._verify_and_exchange(self.assertion(scp="Other.Scope"))
            msal_app.assert_not_called()

    def test_valid_token_is_exchanged_for_search_token(self):
        with patch.object(cloud_app.msal, "ConfidentialClientApplication") as msal_app:
            msal_app.return_value.acquire_token_on_behalf_of.return_value = {"access_token": "delegated"}
            assertion = self.assertion()
            self.assertEqual(cloud_app._verify_and_exchange(assertion), "delegated")
            msal_app.return_value.acquire_token_on_behalf_of.assert_called_once_with(
                user_assertion=assertion,
                scopes=["https://search.azure.com/.default"],
            )

    def test_delegation_failure_rejects_mcp_request(self):
        with patch.object(cloud_app.msal, "ConfidentialClientApplication") as msal_app:
            msal_app.return_value.acquire_token_on_behalf_of.return_value = {"error": "invalid_grant"}
            response = self.client.post(
                "/mcp",
                headers={"Host": "testserver", "Authorization": f"Bearer {self.assertion()}"},
            )
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("invalid_grant", response.text)

    def test_hosted_mode_never_uses_an_unverified_header_as_search_token(self):
        request = SimpleNamespace(
            state=SimpleNamespace(), headers={"authorization": "Bearer unverified"}
        )
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))
        with self.assertRaises(PermissionError):
            server._user_token(ctx)

    def test_delegated_token_reaches_the_mcp_tool(self):
        response = httpx.Response(
            200, json={"references": [], "response": []},
            request=httpx.Request("POST", "https://example.search.windows.net"),
        )
        with (
            patch.object(cloud_app, "_verify_and_exchange", return_value="delegated"),
            patch.object(server, "_headers", new_callable=AsyncMock, return_value={}) as headers,
            patch.object(server.httpx, "AsyncClient") as search_client,
        ):
            search_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
            result = self.client.post(
                "/mcp",
                headers={
                    "Host": "testserver",
                    "Authorization": "Bearer test-assertion",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "search", "arguments": {"query": "example"}},
                },
            )
        self.assertEqual(result.status_code, 200)
        self.assertIn("results", result.text)
        headers.assert_awaited_once_with("delegated")


class HostedSearchHeadersTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_identity_and_user_token_are_both_sent(self):
        with patch.object(
            server._search_credential, "get_token", return_value=SimpleNamespace(token="app-token")
        ):
            headers = await server._headers("user-token")
        self.assertEqual(headers["Authorization"], "Bearer app-token")
        self.assertEqual(headers["x-ms-query-source-authorization"], "user-token")
        self.assertNotIn("api-key", headers)

    async def test_fetch_rejects_an_inaccessible_document(self):
        response = httpx.Response(
            200, json={"value": []},
            request=httpx.Request("POST", "https://example.search.windows.net"),
        )
        with (
            patch.object(server, "_user_token", return_value="user-token"),
            patch.object(server, "_index_name", new_callable=AsyncMock, return_value="index"),
            patch.object(server, "_headers", new_callable=AsyncMock, return_value={}),
            patch.object(server.httpx, "AsyncClient") as search_client,
        ):
            search_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
            with self.assertRaises(PermissionError):
                await server.fetch("inaccessible-id", SimpleNamespace())

    async def test_search_reports_malformed_passages(self):
        response = httpx.Response(
            200, json={"response": [{"content": [{"type": "text", "text": "{"}]}]},
            request=httpx.Request("POST", "https://example.search.windows.net"),
        )
        with (
            patch.object(server, "_user_token", return_value="user-token"),
            patch.object(server, "_headers", new_callable=AsyncMock, return_value={}),
            patch.object(server.httpx, "AsyncClient") as search_client,
        ):
            search_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
            with self.assertRaisesRegex(RuntimeError, "invalid passage data"):
                await server.search("question", SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
