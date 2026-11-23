"""Interactively verify test MCP access for two distinct Entra users."""

import argparse
import asyncio
import json
import re
from uuid import UUID

import httpx
import msal
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

import bootstrap_search as test
import testenv


def _uuid(value):
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError("Client and user IDs must be UUIDs.") from error
    if not parsed.int:
        raise ValueError("Client and user IDs must be nonzero.")
    return str(parsed)


def check_allowed_clients(setting, console_id):
    """The MCP must accept the test console, plus only the Microsoft 365 SSO client when that is enabled."""
    allowed = {value.strip().lower() for value in setting.split(",") if value.strip()}
    if allowed not in ({console_id}, {console_id, testenv.M365_TOKEN_STORE_CLIENT_ID}):
        raise RuntimeError("The test MCP app does not have the expected allowed client IDs.")


def verify_inputs(env):
    if len(env.test_users) != 2:
        raise ValueError("The parameters file must list exactly two test users.")
    console_id, mcp_id, user_a, user_b = map(
        _uuid, (env.console_client_id, env.mcp_client_id, *env.test_users))
    group_a, group_b = test.group_ids(env.group_a, env.group_b)
    if console_id == mcp_id or user_a == user_b:
        raise ValueError("The test applications and the two test users must be distinct.")
    subscription, tenant = test._preflight(env, group_a, group_b)

    def site_id(name):
        return test.resource_id(env, subscription, "Microsoft.Web/sites", name)

    def setting(site, name):
        url = f"https://management.azure.com{site_id(site)}/config/appsettings/list?api-version=2024-11-01"
        return test._az("rest", "--method", "post", "--uri", url, "--subscription", subscription,
                        "--query", f"properties.{name}", "-o", "tsv")

    for client_id, expected in ((console_id, env.console_app), (mcp_id, env.mcp_app)):
        app = json.loads(test._az("ad", "app", "show", "--id", client_id,
                                  "--query", "{appId:appId,displayName:displayName,signInAudience:signInAudience}",
                                  "-o", "json"))
        if (app.get("appId", "").lower() != client_id or app.get("displayName") != expected or
                app.get("signInAudience") != "AzureADMyOrg"):
            raise RuntimeError(f"The supplied client ID is not the expected test application: {expected}.")
    if setting(env.mcp_site, "MCP_CLIENT_ID").lower() != mcp_id:
        raise RuntimeError("The test MCP app does not have the expected test client configuration.")
    check_allowed_clients(setting(env.mcp_site, "MCP_ALLOWED_CLIENT_IDS"), console_id)
    admins = {value.strip().lower() for value in setting(env.console_site, "ADMIN_OBJECT_IDS").split(",")}
    if {user_a, user_b} & admins:
        raise RuntimeError("Choose test users who are not test console administrators.")
    for user, expected, other in ((user_a, group_a, group_b), (user_b, group_b, group_a)):
        for group, wanted in ((expected, "true"), (other, "false")):
            answer = test._az("ad", "group", "member", "check", "--group", group,
                              "--member-id", user, "-o", "tsv").lower()
            if answer != wanted:
                raise RuntimeError(f"Unexpected group membership for test user {user}.")
    host = test._az("resource", "show", "--ids", site_id(env.mcp_site),
                    "--query", "properties.defaultHostName", "-o", "tsv").lower()
    if not env.host_pattern(env.mcp_site).fullmatch(host):
        raise RuntimeError("The MCP site's actual host is not the expected isolated test host.")
    return tenant, console_id, mcp_id, user_a, user_b, f"https://{host}/mcp"


def _login(tenant, client_id, mcp_id, expected_user):
    client = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}")
    flow = client.initiate_device_flow(scopes=[f"api://{mcp_id}/access_as_user"])
    if "user_code" not in flow:
        raise RuntimeError(f"Could not start device login: {flow.get('error', 'unknown_error')}.")
    print(flow["message"], flush=True)
    result = client.acquire_token_by_device_flow(flow)
    if not result.get("access_token"):
        raise RuntimeError(f"Device login failed: {result.get('error', 'unknown_error')}.")
    if result.get("id_token_claims", {}).get("oid", "").lower() != expected_user:
        raise RuntimeError("The signed-in account does not match the selected test user.")
    return result["access_token"]


def _success(result):
    if result.isError:
        raise RuntimeError("An MCP tool returned an error; access was not verified.")
    value = result.structuredContent
    if isinstance(value, dict):
        return value["result"] if isinstance(value.get("result"), dict) else value
    if len(result.content) == 1 and isinstance(result.content[0], TextContent):
        try:
            value = json.loads(result.content[0].text)
        except json.JSONDecodeError as error:
            raise RuntimeError("The MCP tool returned invalid JSON.") from error
        if isinstance(value, dict):
            return value
    raise RuntimeError("The MCP tool returned an unexpected response shape.")


def _expect_denied(result):
    message = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
    if not result.isError or "Document not found or not accessible to this user." not in message:
        raise RuntimeError("The cross-user fetch did not return the expected access denial.")


def _result_for(data, allowed_name, forbidden_name, forbidden_marker, require_allowed):
    entries = data.get("results")
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise RuntimeError("The MCP search response has an unexpected shape.")
    if any(item.get("title") == forbidden_name or forbidden_marker in str(item.get("text", "")) for item in entries):
        raise RuntimeError("The MCP search exposed a document from the other test group.")
    matches = [item for item in entries if item.get("title") == allowed_name]
    if require_allowed and (len(matches) != 1 or not matches[0].get("id")):
        raise RuntimeError(f"Expected test document {allowed_name} was not returned by Search.")
    return matches[0]["id"] if require_allowed else None


def _http_client(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=False)


async def _check_tools(endpoint, token_a, token_b):
    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = await client.get(endpoint, headers=headers)
            if response.status_code != 401:
                raise RuntimeError("The MCP endpoint did not reject a missing or invalid token with HTTP 401.")

    async with streamablehttp_client(endpoint, headers={"Authorization": f"Bearer {token_a}"},
                                     httpx_client_factory=_http_client) as (read_a, write_a, _):
        async with ClientSession(read_a, write_a) as a:
            await a.initialize()
            async with streamablehttp_client(endpoint, headers={"Authorization": f"Bearer {token_b}"},
                                             httpx_client_factory=_http_client) as (read_b, write_b, _):
                async with ClientSession(read_b, write_b) as b:
                    await b.initialize()
                    a_own = _success(await a.call_tool("search", {"query": "ALPHA-4827"}))
                    a_id = _result_for(a_own, "admin-test.txt", "alluser-test.txt", "BRAVO-9631", True)
                    b_own = _success(await b.call_tool("search", {"query": "BRAVO-9631"}))
                    b_id = _result_for(b_own, "alluser-test.txt", "admin-test.txt", "ALPHA-4827", True)
                    _result_for(_success(await a.call_tool("search", {"query": "BRAVO-9631"})),
                                "admin-test.txt", "alluser-test.txt", "BRAVO-9631", False)
                    _result_for(_success(await b.call_tool("search", {"query": "ALPHA-4827"})),
                                "alluser-test.txt", "admin-test.txt", "ALPHA-4827", False)
                    for session, document_id, marker in ((a, a_id, "ALPHA-4827"), (b, b_id, "BRAVO-9631")):
                        document = _success(await session.call_tool("fetch", {"id": document_id}))
                        if marker not in document.get("text", ""):
                            raise RuntimeError("The authorized fetch did not return the expected synthetic content.")
                    _expect_denied(await a.call_tool("fetch", {"id": b_id}))
                    _expect_denied(await b.call_tool("fetch", {"id": a_id}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--params", help=f"Parameters file (default: ${testenv.PARAMS_ENV} or runtime.parameters.json).")
    args = parser.parse_args()
    tenant, console_id, mcp_id, user_a, user_b, endpoint = verify_inputs(testenv.load(args.params))
    print("Review inherited Search permissions for both users before continuing.")
    test.confirm(testenv.load(args.params), "start two interactive test logins")
    print("Sign in as test user A. Do not share the device code or tokens.")
    token_a = _login(tenant, console_id, mcp_id, user_a)
    print("Sign in as test user B in a separate browser session.")
    token_b = _login(tenant, console_id, mcp_id, user_b)
    asyncio.run(_check_tools(endpoint, token_a, token_b))
    print("PASS: missing/invalid tokens rejected; authorized search/fetch and cross-user denials verified.")


if __name__ == "__main__":
    main()
