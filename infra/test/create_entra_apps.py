"""Create and configure the three Entra app registrations of one isolated test environment.

Steps (preview by default, --execute to apply):
  create       before the first deployment: register the console, MCP and download apps,
               their service principals and admin consents, then write the client IDs to the parameters file.
  post-deploy  after the deployment: add the redirect URIs of the real hosts and the MCP federated credential.
  sso          optional: wire the Teams Developer Portal SSO registration for the Microsoft 365 Copilot connector.
"""

import argparse
import json
import os
from pathlib import Path
import tempfile
import uuid

import bootstrap_search as test
import testenv


GRAPH = "https://graph.microsoft.com/v1.0"
# Azure AI Search first-party application and its delegated user_impersonation scope (identical in every tenant).
SEARCH_APP_ID = "880da380-985e-4198-81b9-e05b1cc53158"
SEARCH_SCOPE_ID = "a4165a31-5d9e-4120-bd1e-9d88c66fd3b8"
TEAMS_CONSENT_REDIRECT = "https://teams.microsoft.com/api/platform/v1.0/oAuthConsentRedirect"


def _scope(scope_id, value, admin_name, admin_description, user_description):
    return {
        "id": scope_id, "value": value, "type": "User", "isEnabled": True,
        "adminConsentDisplayName": admin_name, "adminConsentDescription": admin_description,
        "userConsentDisplayName": admin_name, "userConsentDescription": user_description,
    }


def _search_access():
    return {"resourceAppId": SEARCH_APP_ID, "resourceAccess": [{"id": SEARCH_SCOPE_ID, "type": "Scope"}]}


def mcp_app_body(env, scope_id):
    return {
        "displayName": env.mcp_app,
        "signInAudience": "AzureADMyOrg",
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [_scope(
                scope_id, "access_as_user", "Access ShareIARAG test MCP",
                "Access the isolated ShareIARAG test MCP service as the signed-in user.",
                "Access ShareIARAG test documents on your behalf.",
            )],
        },
        "requiredResourceAccess": [_search_access()],
    }


def console_app_body(env, scope_id, mcp_client_id, mcp_scope_id):
    return {
        "displayName": env.console_app,
        "signInAudience": "AzureADMyOrg",
        "isFallbackPublicClient": True,
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [_scope(
                scope_id, "Console.Access", "Access ShareIARAG test console",
                "Access the isolated ShareIARAG test console.", "Access the isolated ShareIARAG test console.",
            )],
        },
        "requiredResourceAccess": [
            {"resourceAppId": mcp_client_id, "resourceAccess": [{"id": mcp_scope_id, "type": "Scope"}]},
            _search_access(),
        ],
    }


def download_app_body(env):
    return {
        "displayName": env.download_app,
        "signInAudience": "AzureADMyOrg",
        "groupMembershipClaims": "SecurityGroup",
        # Easy Auth signs users in with an ID token; without it sign-in fails with AADSTS700054.
        "web": {"implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False}},
    }


def preauthorize(api, client_id, scope_id):
    """Return the api block with client_id pre-authorized for scope_id, keeping existing entries."""
    entries = [entry for entry in api.get("preAuthorizedApplications", []) if entry["appId"].lower() != client_id]
    entries.append({"appId": client_id, "delegatedPermissionIds": [scope_id]})
    return {**api, "preAuthorizedApplications": entries}


def federated_credential_body(env, tenant, principal_id):
    return {
        "name": env.mcp_identity,
        "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "subject": principal_id,
        "audiences": ["api://AzureADTokenExchange"],
    }


def with_item(values, item):
    return values if item in values else [*values, item]


def graph(method, path, body=None):
    arguments = ["rest", "--method", method, "--url", GRAPH + path, "--headers", "Content-Type=application/json"]
    body_file = None
    if body is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(body, handle)
            body_file = handle.name
        arguments += ["--body", "@" + body_file]
    try:
        output = test._az(*arguments)
    finally:
        if body_file:
            os.unlink(body_file)
    return json.loads(output) if output else None


def _find_app(display_name):
    found = graph("GET", f"/applications?$filter=displayName eq '{display_name}'&$select=id,appId,displayName")
    return found["value"]


def _app(client_id):
    found = graph("GET", f"/applications?$filter=appId eq '{client_id}'")["value"]
    if len(found) != 1:
        raise RuntimeError(f"App registration not found: {client_id}.")
    return found[0]


def _service_principal(client_id):
    found = graph("GET", f"/servicePrincipals?$filter=appId eq '{client_id}'&$select=id")["value"]
    if found:
        return found[0]["id"]
    return graph("POST", "/servicePrincipals", {"appId": client_id})["id"]


def _create_app(body):
    app = graph("POST", "/applications", body)
    if "api" in body:
        graph("PATCH", f"/applications/{app['id']}", {"identifierUris": [f"api://{app['appId']}"]})
    print(f"Created {body['displayName']}: {app['appId']}")
    return app


def _grant(client_sp, resource_sp, scope):
    graph("POST", "/oauth2PermissionGrants", {
        "clientId": client_sp, "consentType": "AllPrincipals", "resourceId": resource_sp, "scope": scope,
    })


def _is_placeholder(value):
    return not value or value.startswith("<")


def write_parameters(path, updates):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for name, value in updates.items():
        data["parameters"][name] = {"value": value}
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def create(env, params, execute):
    for client_id in (env.console_client_id, env.mcp_client_id, env.download_client_id):
        if not _is_placeholder(client_id):
            raise RuntimeError("The parameters file already holds client IDs; refusing to create new apps.")
    names = (env.mcp_app, env.console_app, env.download_app)
    if not execute:
        print("Preview only: would create " + ", ".join(names) + ", their service principals and admin consents.")
        return
    existing = [name for name in names if _find_app(name)]
    if existing:
        raise RuntimeError("App registrations already exist, inspect them first: " + ", ".join(existing))
    test.confirm(env, "create the Entra app registrations")

    search_sp = graph("GET", f"/servicePrincipals?$filter=appId eq '{SEARCH_APP_ID}'&$select=id")["value"]
    if not search_sp:
        raise RuntimeError("The Azure AI Search service principal is missing from this tenant.")
    search_sp = search_sp[0]["id"]

    mcp_scope, console_scope = str(uuid.uuid4()), str(uuid.uuid4())
    mcp = _create_app(mcp_app_body(env, mcp_scope))
    console = _create_app(console_app_body(env, console_scope, mcp["appId"], mcp_scope))
    download = _create_app(download_app_body(env))
    mcp_api = graph("GET", f"/applications/{mcp['id']}?$select=api")["api"]
    graph("PATCH", f"/applications/{mcp['id']}", {"api": preauthorize(mcp_api, console["appId"], mcp_scope)})

    mcp_sp = _service_principal(mcp["appId"])
    console_sp = _service_principal(console["appId"])
    _service_principal(download["appId"])
    _grant(console_sp, search_sp, "user_impersonation")
    _grant(console_sp, mcp_sp, "access_as_user")
    _grant(mcp_sp, search_sp, "user_impersonation")

    write_parameters(params, {
        "consoleClientId": console["appId"], "mcpClientId": mcp["appId"], "downloadClientId": download["appId"],
    })
    print(f"Client IDs written to {params}.")


def _host(env, site):
    host = test._az("resource", "show", "-g", env.resource_group, "-n", site, "--resource-type",
                    "Microsoft.Web/sites", "--query", "properties.defaultHostName", "-o", "tsv")
    if not env.host_pattern(site).fullmatch(host):
        raise RuntimeError(f"Unexpected host for {site}: {host}.")
    return host


def post_deploy(env, execute):
    _, tenant = test.verify_test_scope(env)
    console_host = _host(env, env.console_site)
    download_host = _host(env, env.download_site)
    principal = test._az("identity", "show", "-g", env.resource_group, "-n", env.mcp_identity,
                         "--query", "principalId", "-o", "tsv")
    console_redirect = f"https://{console_host}/auth/callback"
    download_redirect = f"https://{download_host}/.auth/login/aad/callback"
    if not execute:
        print(f"Preview only: SPA redirect {console_redirect}, web redirect {download_redirect}, "
              f"federated credential {env.mcp_identity} -> {principal}.")
        return
    test.confirm(env, "update the Entra app registrations")

    console = _app(env.console_client_id)
    graph("PATCH", f"/applications/{console['id']}",
          {"spa": {"redirectUris": with_item(console["spa"]["redirectUris"], console_redirect)}})
    download = _app(env.download_client_id)
    graph("PATCH", f"/applications/{download['id']}",
          {"web": {"redirectUris": with_item(download["web"]["redirectUris"], download_redirect)}})
    mcp = _app(env.mcp_client_id)
    credentials = graph("GET", f"/applications/{mcp['id']}/federatedIdentityCredentials")["value"]
    if not any(credential["subject"] == principal for credential in credentials):
        graph("POST", f"/applications/{mcp['id']}/federatedIdentityCredentials",
              federated_credential_body(env, tenant, principal))
    print("Redirect URIs and federated credential configured.")


def sso(env, params, app_id_uri, execute):
    if not app_id_uri.startswith("api://"):
        raise ValueError("The Application ID URI must start with api://.")
    if not execute:
        print(f"Preview only: add {app_id_uri}, the Teams consent redirect and the Microsoft 365 client to "
              f"{env.mcp_app}, then write mcpSsoAudience.")
        return
    test.confirm(env, "enable Microsoft 365 SSO on the MCP app")
    mcp = _app(env.mcp_client_id)
    scope_id = next(scope["id"] for scope in mcp["api"]["oauth2PermissionScopes"] if scope["value"] == "access_as_user")
    graph("PATCH", f"/applications/{mcp['id']}", {
        "identifierUris": with_item(mcp["identifierUris"], app_id_uri),
        "web": {"redirectUris": with_item(mcp["web"]["redirectUris"], TEAMS_CONSENT_REDIRECT)},
        "api": preauthorize(mcp["api"], testenv.M365_TOKEN_STORE_CLIENT_ID, scope_id),
    })
    write_parameters(params, {"mcpSsoAudience": app_id_uri})
    print(f"MCP app updated; mcpSsoAudience written to {params}. Redeploy the Bicep template next.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("create", "post-deploy", "sso"))
    parser.add_argument("--params", help="Parameters file (default: SHAREIARAG_TEST_PARAMS or runtime.parameters.json).")
    parser.add_argument("--app-id-uri", help="Application ID URI from the Teams Developer Portal (sso step).")
    parser.add_argument("--execute", action="store_true", help="Apply the changes instead of previewing them.")
    args = parser.parse_args()
    params = Path(args.params or os.environ.get(testenv.PARAMS_ENV) or testenv.DEFAULT_PARAMS)
    env = testenv.load(params)
    if args.step == "create":
        create(env, params, args.execute)
    elif args.step == "post-deploy":
        post_deploy(env, args.execute)
    else:
        if not args.app_id_uri:
            parser.error("--app-id-uri is required for the sso step.")
        sso(env, params, args.app_id_uri, args.execute)


if __name__ == "__main__":
    main()
