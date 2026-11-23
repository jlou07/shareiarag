"""Initialize the test Foundry connection and agent after Search is ready."""

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

import bootstrap_search as test
from build_agent_instructions import SOURCE, render_instructions
import testenv


AGENT = "agent-shareiarag-test"
CONNECTION = "af-kb-all-mcp"


def kb_endpoint(env):
    return f"{env.search_endpoint}/knowledgebases/af-kb-all/mcp?api-version={test.API_VERSION}"


def project_endpoint(env):
    return f"https://{env.foundry}.services.ai.azure.com/api/projects/{env.project}"


def connection_body(env):
    return {"properties": {
        "category": "RemoteTool", "authType": "ProjectManagedIdentity", "audience": "https://search.azure.com/",
        "target": kb_endpoint(env), "isSharedToAll": False, "metadata": {"ApiType": "Azure"},
    }}


def agent_body(env, source: str, download_base: str):
    return {"definition": {
        "kind": "prompt",
        "model": "gpt-5-mini",
        "instructions": render_instructions(env, source, download_base),
        "tools": [{
            "type": "mcp",
            "server_label": "knowledge-base",
            "server_url": kb_endpoint(env),
            "project_connection_id": CONNECTION,
            "require_approval": "never",
            "allowed_tools": {"tool_names": ["knowledge_base_retrieve"]},
        }],
    }}


def _request(method, url, token, body=None, allow_missing=False):
    headers = {"Authorization": f"Bearer {token}"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["If-None-Match"] = "*"
        payload = json.dumps(body).encode("utf-8")
    try:
        with build_opener(test._NoRedirect).open(Request(url, data=payload, headers=headers, method=method),
                                                timeout=60) as response:
            return response.status
    except HTTPError as error:
        if allow_missing and error.code == 404:
            return 404
        raise RuntimeError(f"Foundry {method} failed with HTTP {error.code}.") from None
    except (URLError, OSError) as error:
        raise RuntimeError("Could not complete the Foundry request.") from error


def initialize(env):
    subscription, _ = test.verify_test_scope(env)
    project_id = test.resource_id(env, subscription, "Microsoft.CognitiveServices/accounts",
                                  f"{env.foundry}/projects/{env.project}")
    endpoint = project_endpoint(env)
    project = json.loads(test._az("resource", "show", "--ids", project_id, "-o", "json"))
    if (project.get("id", "").lower() != project_id.lower() or
            project.get("properties", {}).get("endpoints", {}).get("AI Foundry API") != endpoint):
        raise RuntimeError("The Foundry project endpoint does not match the isolated test project.")
    function_id = test.resource_id(env, subscription, "Microsoft.Web/sites", env.download_site)
    host = test._az("resource", "show", "--ids", function_id, "--query", "properties.defaultHostName", "-o", "tsv")
    body = agent_body(env, SOURCE.read_text(encoding="utf-8"), f"https://{host}/api/f")

    def token(resource):
        return test._az("account", "get-access-token", "--subscription", subscription,
                        "--resource", resource, "--query", "accessToken", "-o", "tsv")

    test._request(env, "GET", "/knowledgebases/af-kb-all", token("https://search.azure.com"))
    arm_token = token("https://management.azure.com")
    foundry_token = token("https://ai.azure.com")
    connection_url = (f"https://management.azure.com{project_id}/connections/{CONNECTION}"
                      "?api-version=2025-10-01-preview")
    if _request("GET", connection_url, arm_token, allow_missing=True) != 404:
        raise RuntimeError("The test Foundry connection already exists; inspect it rather than overwriting.")
    if _request("GET", f"{endpoint}/agents/{AGENT}?api-version=v1", foundry_token, allow_missing=True) != 404:
        raise RuntimeError("The test Foundry agent already exists; inspect it rather than creating a version.")
    try:
        _request("PUT", connection_url, arm_token, connection_body(env))
    except RuntimeError as error:
        raise RuntimeError("The connection may have been created; inspect the test project before retrying.") from error
    try:
        _request("POST", f"{endpoint}/agents/{AGENT}/versions?api-version=v1", foundry_token, body)
    except RuntimeError as error:
        raise RuntimeError(
            "The connection was created and the agent may have been created. "
            "Inspect both in the test project before retrying."
        ) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--params", help=f"Parameters file (default: ${testenv.PARAMS_ENV} or runtime.parameters.json).")
    parser.add_argument("--execute", action="store_true", help="Create the test connection and agent after approval.")
    args = parser.parse_args()
    env = testenv.load(args.params)
    if not args.execute:
        print(f"Preview only: connection {CONNECTION}, agent {AGENT}, target {kb_endpoint(env)}")
        return
    print("Do not initialize the same project concurrently; existence checks are not atomic.")
    test.confirm(env, "initialize the test Foundry project")
    initialize(env)
    print("Created the test connection and agent. Verify delegated access separately for both users.")


if __name__ == "__main__":
    main()
