"""Initialize the isolated test Search service from the reviewed local definitions."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

import testenv


API_VERSION = "2026-08-01-preview"
SOURCE = Path(__file__).resolve().parents[2] / "index-azurefiles.http"
TARGETS = {
    1: "/datasources/af-ds-admin",
    8: "/datasources/af-ds-alluser",
    19: "/indexes/af-index-all",
    20: "/skillsets/af-skills-all-admin",
    24: "/skillsets/af-skills-all-alluser",
    21: "/indexers/af-indexer-all-admin",
    25: "/indexers/af-indexer-all-alluser",
    29: "/knowledgesources/af-ks-all",
    30: "/knowledgebases/af-kb-all",
}
PLACEHOLDER = re.compile(r"\{\{\$dotenv ([A-Z_]+)\}\}")


def group_ids(group_a: str, group_b: str) -> tuple[str, str]:
    try:
        first, second = UUID(group_a), UUID(group_b)
    except (ValueError, AttributeError) as error:
        raise ValueError("Both group IDs must be UUIDs.") from error
    if not first.int or not second.int or first == second:
        raise ValueError("The two group IDs must be nonzero and distinct.")
    return str(first), str(second)


def validate_connection(env, connection: str) -> None:
    parts = [part.split("=", 1) for part in connection.split(";") if part]
    if any(len(part) != 2 for part in parts):
        raise ValueError("The test storage connection string is malformed.")
    values = dict(parts)
    if (
        len(parts) != len(values)
        or set(values) != {"DefaultEndpointsProtocol", "AccountName", "AccountKey", "EndpointSuffix"}
        or values["DefaultEndpointsProtocol"] != "https"
        or values["AccountName"] != env.files
        or not values["AccountKey"]
        or values["EndpointSuffix"] != "core.windows.net"
    ):
        raise ValueError("The vault secret is not a connection string for the isolated test storage account.")


def _substitute(value, variables):
    if isinstance(value, dict):
        return {key: _substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, variables) for item in value]
    if isinstance(value, str):
        def replace(match):
            name = match.group(1)
            if name not in variables:
                raise ValueError(f"Unknown template variable: {name}.")
            return variables[name]
        result = PLACEHOLDER.sub(replace, value)
        if "{{$dotenv" in result:
            raise ValueError("An unresolved template variable remains.")
        return result
    return value


def _named(items, name):
    matches = [item for item in items if item.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {name} item in the Search template.")
    return matches[0]


def _verify_skillset(env, body, group, share):
    skills = body["skills"]
    group_skill = _named(skills, "group-id")
    group_input = _named(group_skill["inputs"], "whenTrue")
    group_condition = _named(group_skill["inputs"], "condition")
    group_output = _named(group_skill["outputs"], "output")
    group_array = _named(skills, "group-ids-array")
    share_skill = _named(skills, "share-name")
    embedding = _named(skills, "embedding")
    selectors = body["indexProjections"]["selectors"]
    if (
        group_skill["@odata.type"] != "#Microsoft.Skills.Util.ConditionalSkill"
        or group_condition["source"] != "= true"
        or group_input["source"] != f"= '{group}'"
        or group_output["targetName"] != "group_id"
        or _named(group_array["inputs"], "text")["source"] != "/document/group_id"
        or _named(group_array["outputs"], "textItems")["targetName"] != "group_ids"
        or _named(share_skill["inputs"], "condition")["source"] != "= true"
        or _named(share_skill["inputs"], "whenTrue")["source"] != f"= '{share}'"
        or _named(share_skill["outputs"], "output")["targetName"] != "share"
        or embedding["resourceUri"] != env.foundry_endpoint
        or embedding["deploymentId"] != "text-embedding-3-small"
        or len(selectors) != 1
        or selectors[0]["targetIndexName"] != "af-index-all"
        or _named(selectors[0]["mappings"], "group_ids")["source"] != "/document/group_ids"
        or _named(selectors[0]["mappings"], "share")["source"] != "/document/share"
    ):
        raise ValueError("The Search skillset no longer preserves the intended test group mapping.")


def render_requests(env, source: str, group_a: str, group_b: str, connection: str):
    group_a, group_b = group_ids(group_a, group_b)
    validate_connection(env, connection)
    variables = {
        "ADMIN_GROUP_ID": group_a,
        "ALLUSER_GROUP_ID": group_b,
        "FILES_CONNECTION_STRING": connection,
        "AOAI_ENDPOINT": env.foundry_endpoint,
        "AOAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-small",
    }
    markers = list(re.finditer(r"(?m)^###[^\r\n]*\r?\n", source))
    bodies = {}
    for index, marker in enumerate(markers):
        number = re.match(r"###\s+(\d+)\.", marker.group())
        if not number or int(number.group(1)) not in TARGETS:
            continue
        number = int(number.group(1))
        if number in bodies:
            raise ValueError(f"Search request {number} is duplicated.")
        end = markers[index + 1].start() if index + 1 < len(markers) else len(source)
        section = source[marker.end():end]
        request = re.search(r"(?m)^PUT[ \t]+([^\r\n]+)", section)
        expected = "{{$dotenv SEARCH_ENDPOINT}}" + TARGETS[number] + f"?api-version={API_VERSION}"
        if not request or request.group(1).strip() != expected:
            raise ValueError(f"Search request {number} has an unexpected target.")
        body_start = re.search(r"(?m)^\s*\{", section[request.end():])
        if not body_start:
            raise ValueError(f"Search request {number} has no JSON body.")
        body_text = section[request.end() + body_start.start():].lstrip()
        body, _ = json.JSONDecoder().raw_decode(body_text)
        body = _substitute(body, variables)
        if body.get("name") != TARGETS[number].rsplit("/", 1)[-1]:
            raise ValueError(f"Search request {number} has an unexpected resource name.")
        bodies[number] = body
    if set(bodies) != set(TARGETS):
        raise ValueError("The reviewed Search definitions are incomplete.")

    index = bodies[19]
    permission_fields = [field for field in index["fields"] if "permissionFilter" in field]
    if (
        index.get("permissionFilterOption") != "enabled"
        or len(permission_fields) != 1
        or permission_fields[0].get("name") != "group_ids"
        or permission_fields[0].get("type") != "Collection(Edm.String)"
        or permission_fields[0].get("permissionFilter") != "groupIds"
        or permission_fields[0].get("filterable") is not True
        or _named(index["vectorSearch"]["vectorizers"], "aoai-vectorizer")["azureOpenAIParameters"]["resourceUri"] != env.foundry_endpoint
    ):
        raise ValueError("The Search index no longer enforces the intended group filter.")
    for data_source, share in ((bodies[1], "admin-share"), (bodies[8], "alluser-share")):
        if (
            data_source.get("type") != "azurefile"
            or data_source["container"].get("name") != share
            or data_source["credentials"].get("connectionString") != connection
        ):
            raise ValueError("A Search data source does not target the expected test share.")
    _verify_skillset(env, bodies[20], group_a, "admin-share")
    _verify_skillset(env, bodies[24], group_b, "alluser-share")
    for number, name in ((21, "admin"), (25, "alluser")):
        item = bodies[number]
        if (
            item.get("dataSourceName") != f"af-ds-{name}"
            or item.get("skillsetName") != f"af-skills-all-{name}"
            or item.get("targetIndexName") != "af-index-all"
            or item.get("disabled", False)
        ):
            raise ValueError("A Search indexer does not target the expected test source and index.")
    if (
        bodies[29].get("kind") != "searchIndex"
        or bodies[29]["searchIndexParameters"].get("searchIndexName") != "af-index-all"
        or [item["name"] for item in bodies[30]["knowledgeSources"]] != ["af-ks-all"]
        or len(bodies[30]["models"]) != 1
        or bodies[30]["models"][0]["azureOpenAIParameters"]["resourceUri"] != env.foundry_endpoint
        or bodies[30]["models"][0]["azureOpenAIParameters"]["deploymentId"] != "gpt-5-mini"
    ):
        raise ValueError("The knowledge source or knowledge base has an unexpected target.")
    bodies[30]["retrievalReasoningEffort"] = {"kind": "medium"}
    return [(TARGETS[number], bodies[number]) for number in TARGETS]


def _az(*arguments):
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is not available on PATH.")
    result = subprocess.run(
        [executable, *arguments, "--only-show-errors"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def account():
    """Return the subscription and tenant currently selected in Azure CLI."""
    current = json.loads(_az("account", "show", "--query", "{id:id,tenantId:tenantId}", "-o", "json"))
    if not current.get("id") or not current.get("tenantId"):
        raise RuntimeError("Azure CLI has no selected subscription; run az login and az account set.")
    return current["id"], current["tenantId"]


def resource_id(env, subscription, provider, name):
    return f"/subscriptions/{subscription}/resourceGroups/{env.resource_group}/providers/{provider}/{name}"


def verify_test_scope(env):
    """Check that the selected subscription holds this environment's resources; return (subscription, tenant)."""
    subscription, tenant = account()
    resources = (
        ("Microsoft.Search/searchServices", env.search),
        ("Microsoft.CognitiveServices/accounts", env.foundry),
        ("Microsoft.Storage/storageAccounts", env.files),
        ("Microsoft.KeyVault/vaults", env.vault),
    )
    for provider, name in resources:
        expected = resource_id(env, subscription, provider, name)
        actual = _az("resource", "show", "--ids", expected, "--query", "id", "-o", "tsv")
        if actual.lower() != expected.lower():
            raise RuntimeError(f"The expected test resource was not found: {name}.")
    return subscription, tenant


def confirm(env, action):
    subscription, _ = account()
    prompt = f"Subscription {subscription}. Type {env.resource_group} to {action}: "
    if input(prompt).strip() != env.resource_group:
        raise RuntimeError("Confirmation did not match; no changes were made.")


def _preflight(env, group_a, group_b):
    scope = verify_test_scope(env)
    for group_id in (group_a, group_b):
        group = json.loads(_az("ad", "group", "show", "--group", group_id, "--query",
                               "{id:id,securityEnabled:securityEnabled}", "-o", "json"))
        if group.get("id", "").lower() != group_id or group.get("securityEnabled") is not True:
            raise RuntimeError(f"The selected group is not an existing security group: {group_id}.")
    return scope


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(env, method, path, token, body=None, allow_missing=False):
    url = f"{env.search_endpoint}{path}?api-version={API_VERSION}"
    headers = {"Authorization": f"Bearer {token}"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers.update({"Content-Type": "application/json", "If-None-Match": "*"})
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with build_opener(_NoRedirect).open(request, timeout=60) as response:
            return response.status
    except HTTPError as error:
        if allow_missing and error.code == 404:
            return 404
        raise RuntimeError(f"Search {method} {path} failed with HTTP {error.code}; inspect the test service.") from None
    except URLError as error:
        raise RuntimeError(f"Search {method} {path} could not reach the test service.") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--params", help=f"Parameters file (default: ${testenv.PARAMS_ENV} or runtime.parameters.json).")
    parser.add_argument("--execute", action="store_true", help="Create the Search resources after approval.")
    args = parser.parse_args()
    env = testenv.load(args.params)
    group_a, group_b = group_ids(env.group_a, env.group_b)
    template = SOURCE.read_text(encoding="utf-8")

    if not args.execute:
        sample = f"DefaultEndpointsProtocol=https;AccountName={env.files};AccountKey=preview-only;EndpointSuffix=core.windows.net"
        requests = render_requests(env, template, group_a, group_b, sample)
        print(f"Preview only: {env.search_endpoint}")
        for path, _ in requests:
            print(f"  create {path}")
        return

    confirm(env, "initialize this test Search service")
    subscription, _ = _preflight(env, group_a, group_b)
    connection = _az("keyvault", "secret", "show", "--vault-name", env.vault,
                     "--name", f"files-connection-{env.files}", "--query", "value", "-o", "tsv")
    requests = render_requests(env, template, group_a, group_b, connection)
    token = _az("account", "get-access-token", "--subscription", subscription,
                "--resource", "https://search.azure.com", "--query", "accessToken", "-o", "tsv")
    for path, _ in requests:
        if _request(env, "GET", path, token, allow_missing=True) != 404:
            raise RuntimeError(f"{path} already exists; inspect the test service instead of overwriting it.")
    for path, body in requests:
        status = _request(env, "PUT", path, token, body)
        print(f"Created {path} (HTTP {status}).")
    print("Search definitions created. Check both indexer results and test access with two separate users.")


if __name__ == "__main__":
    main()
